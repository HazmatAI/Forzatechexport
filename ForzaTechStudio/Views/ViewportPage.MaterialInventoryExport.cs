using ForzaTechStudio.Services;
using ForzaTechStudio.ViewModels.ThreeDViewer;
using ForzaTools.Bundles.Blobs;
using Microsoft.UI.Xaml;
using Microsoft.UI.Xaml.Controls;
using System;
using System.Collections.Generic;
using System.IO;
using System.Linq;
using System.Numerics;
using System.Security.Cryptography;
using System.Text.Json;
using System.Text.Json.Nodes;
using System.Threading.Tasks;
using Windows.Storage.Pickers;

namespace ForzaTechStudio.Views
{
    public sealed partial class ViewportPage : Page
    {
        private async void SaveAllMaterialsJson_Click(object sender, RoutedEventArgs e)
        {
            try
            {
                await SaveAllMaterialsJsonAsync();
            }
            catch (Exception ex)
            {
                App.ShowErrorDialog($"Failed to build the material inventory: {ex.Message}");
            }
        }

        private async Task SaveAllMaterialsJsonAsync()
        {
            var selectedNodes = FileTree.SelectedItems
                .Cast<object>()
                .Select(item => TryGetViewerNode(item, out var node) ? node : null)
                .Where(node => node != null)
                .Cast<IViewerNode>()
                .Distinct()
                .ToList();

            if (selectedNodes.Count == 0 && ViewModel.SelectedNode != null)
                selectedNodes.Add(ViewModel.SelectedNode);

            if (selectedNodes.Count == 0)
            {
                App.ShowErrorDialog("Select the vehicle, one or more parts, or one or more meshes before exporting materials.");
                return;
            }

            var fullModelBins = new HashSet<ModelBinNode>();
            var meshes = new HashSet<MeshNode>();
            foreach (var node in selectedNodes)
                CollectMaterialExportScope(node, fullModelBins, meshes);

            if (fullModelBins.Count == 0 && meshes.Count == 0)
            {
                App.ShowErrorDialog("The selection does not contain any loaded modelbin geometry.");
                return;
            }

            var document = BuildMaterialInventoryJson(selectedNodes, fullModelBins, meshes);
            int materialCount = document["summary"]?["materialCount"]?.GetValue<int>() ?? 0;
            if (materialCount == 0)
            {
                App.ShowErrorDialog("No materials were found in the selected vehicle parts.");
                return;
            }

            var picker = new FileSavePicker();
            WinRT.Interop.InitializeWithWindow.Initialize(
                picker,
                WinRT.Interop.WindowNative.GetWindowHandle(App.MainWindow));
            picker.SuggestedStartLocation = PickerLocationId.DocumentsLibrary;
            picker.FileTypeChoices.Add("JSON File", new List<string> { ".json" });
            picker.SuggestedFileName = BuildMaterialInventoryFileName(selectedNodes);

            var file = await picker.PickSaveFileAsync();
            if (file == null)
                return;

            try
            {
                var sourceZipPaths = fullModelBins
                    .Concat(meshes.Select(mesh => mesh.ParentModelBin))
                    .Where(modelBin => modelBin != null && !string.IsNullOrWhiteSpace(modelBin.SourceZipPath))
                    .Select(modelBin => modelBin.SourceZipPath!)
                    .Distinct(StringComparer.OrdinalIgnoreCase)
                    .ToList();

                var packageResult = await ExportCompleteVehicleTexturePackageAsync(
                    document,
                    file.Path,
                    sourceZipPaths);

                var options = new JsonSerializerOptions { WriteIndented = true };
                await File.WriteAllTextAsync(file.Path, document.ToJsonString(options));

                int partCount = document["summary"]?["partCount"]?.GetValue<int>() ?? 0;
                App.ShowInfoDialog(
                    $"Saved {materialCount} materials used by {partCount} parts.\n" +
                    $"Exported {packageResult.ExportedTextureCount} PNG texture(s); " +
                    $"{packageResult.UnresolvedTextureCount} unresolved reference(s).\n\n" +
                    $"{file.Path}\n{packageResult.PackageDirectory}",
                    "Material Inventory Saved");
            }
            catch (Exception ex)
            {
                App.ShowErrorDialog($"Failed to save the material inventory: {ex.Message}");
            }
        }

        private static void CollectMaterialExportScope(
            IViewerNode node,
            HashSet<ModelBinNode> fullModelBins,
            HashSet<MeshNode> meshes)
        {
            switch (node)
            {
                case MeshNode mesh:
                    meshes.Add(mesh);
                    return;
                case ModelBinNode modelBin:
                    fullModelBins.Add(modelBin);
                    CollectDescendantGeometry(modelBin, meshes);
                    return;
            }

            foreach (var child in node.Children)
                CollectMaterialExportScope(child, fullModelBins, meshes);
        }

        private static void CollectDescendantGeometry(
            IViewerNode node,
            HashSet<MeshNode> meshes)
        {
            foreach (var child in node.Children)
            {
                if (child is MeshNode mesh)
                    meshes.Add(mesh);

                CollectDescendantGeometry(child, meshes);
            }
        }

        private static JsonObject BuildMaterialInventoryJson(
            IReadOnlyCollection<IViewerNode> selectedNodes,
            IReadOnlyCollection<ModelBinNode> fullModelBins,
            IReadOnlyCollection<MeshNode> meshes)
        {
            var materialsByKey = new Dictionary<string, JsonObject>(StringComparer.OrdinalIgnoreCase);
            var parts = new JsonArray();

            foreach (var mesh in meshes.OrderBy(mesh => BuildNodePath(mesh), StringComparer.OrdinalIgnoreCase))
                AddGeometryMaterialUsage(mesh, mesh.ParentModelBin, mesh.GeometryData, materialsByKey, parts);

            foreach (var modelBin in fullModelBins)
            {
                if (modelBin.Bundle == null)
                    continue;

                foreach (var material in modelBin.Bundle.Blobs.OfType<MaterialBlob>())
                    EnsureMaterial(material, modelBin, materialsByKey);
            }

            var materials = new JsonArray();
            var compactIdByKey = new Dictionary<string, string>(StringComparer.OrdinalIgnoreCase);
            int materialIndex = 1;
            foreach (var pair in materialsByKey.OrderBy(pair => pair.Value["name"]?.GetValue<string>(), StringComparer.OrdinalIgnoreCase))
            {
                string compactId = $"mat_{materialIndex++:D3}";
                compactIdByKey[pair.Key] = compactId;
                pair.Value.Remove("key");
                pair.Value["id"] = compactId;
                materials.Add(pair.Value);
            }

            foreach (JsonNode? node in parts)
            {
                if (node is JsonObject part &&
                    part["material"]?.GetValue<string>() is string materialKey &&
                    compactIdByKey.TryGetValue(materialKey, out string? compactId))
                    part["material"] = compactId;
            }

            return new JsonObject
            {
                ["schema"] = "ForzaTechStudio.MaterialInventory",
                ["schemaVersion"] = 3,
                ["exportedAtUtc"] = DateTime.UtcNow.ToString("O"),
                ["summary"] = new JsonObject
                {
                    ["modelbinCount"] = fullModelBins
                        .Concat(meshes.Select(mesh => mesh.ParentModelBin))
                        .Where(modelBin => modelBin != null)
                        .Select(modelBin => modelBin.FilePath ?? modelBin.ZipEntryName ?? modelBin.Name)
                        .Distinct(StringComparer.OrdinalIgnoreCase)
                        .Count(),
                    ["partCount"] = parts.Count,
                    ["materialCount"] = materials.Count
                },
                ["parts"] = parts,
                ["materials"] = materials
            };
        }

        private static void AddGeometryMaterialUsage(
            IViewerNode geometryNode,
            ModelBinNode modelBin,
            ForzaGeometryData? geometryData,
            Dictionary<string, JsonObject> materialsByKey,
            JsonArray parts)
        {
            MeshBlob? meshBlob = geometryData?.SourceMesh;
            if (modelBin?.Bundle == null || meshBlob == null)
                return;

            short materialId = GetAssignedMaterialIdForExport(meshBlob);
            var material = modelBin.Bundle.Blobs
                .OfType<MaterialBlob>()
                .FirstOrDefault(candidate => (short)GetMaterialIdForExport(candidate) == materialId);

            string? materialKey = null;
            if (material != null)
                materialKey = EnsureMaterial(material, modelBin, materialsByKey);

            parts.Add(new JsonObject
            {
                ["mesh"] = geometryNode.Name,
                ["meshPath"] = BuildNodePath(geometryNode),
                ["model"] = modelBin.Name,
                ["modelSource"] = modelBin.ZipEntryName ?? modelBin.FilePath ?? modelBin.Name,
                // This is the exact slot identifier emitted by the current OBJ/FBX exporters.
                ["unrealMaterialSlot"] = ObjExportService.GetExportMaterialSlotName(
                    GetUnrealExportModelIdentity(modelBin), geometryData),
                ["material"] = materialKey,
                ["materialBinding"] = new JsonObject
                {
                    ["primaryMaterialId"] = materialId,
                    ["primaryMaterialIdHex"] = $"0x{unchecked((ushort)materialId):X4}",
                    ["materialGroups"] = new JsonArray((meshBlob.MaterialGroups ?? [])
                        .Select(group => new JsonArray(group.Select(id => JsonValue.Create(id)).ToArray()))
                        .ToArray()),
                    ["activeMaterialGroup"] = 0,
                    ["activeMaterialGroupIndex"] = meshBlob.MaterialIds != null && meshBlob.MaterialIds.Length > 1 ? 1 : 0
                },
                ["renderHints"] = new JsonObject
                {
                    ["transparent"] = meshBlob.IsTransparent,
                    ["decal"] = meshBlob.IsDecal,
                    ["alphaToCoverage"] = meshBlob.IsAlphaToCoverage,
                    ["lodMin"] = meshBlob.LODLevel1,
                    ["lodMax"] = meshBlob.LODLevel2
                }
            });
        }

        private static string EnsureMaterial(
            MaterialBlob material,
            ModelBinNode modelBin,
            Dictionary<string, JsonObject> materialsByKey)
        {
            uint materialId = GetMaterialIdForExport(material);
            string materialName = MaterialExtractionService.GetMaterialName(material);
            if (string.IsNullOrWhiteSpace(materialName))
                materialName = "unnamed_material";

            string materialPath = material.Bundle?.Blobs
                .OfType<MaterialResourceBlob>()
                .FirstOrDefault()?.Path ?? string.Empty;
            string contentHash = GetMaterialContentFingerprint(material, modelBin, materialId);
            string key = $"{materialPath}|{materialName}|{contentHash}";

            if (!materialsByKey.ContainsKey(key))
                materialsByKey[key] = CreateMaterialJson(key, material, modelBin, materialName, materialId);

            return key;
        }

        private static string GetMaterialContentFingerprint(
            MaterialBlob material,
            ModelBinNode modelBin,
            uint materialId)
        {
            try
            {
                byte[]? contents = material.GetContents();
                if (contents is { Length: > 0 })
                    return Convert.ToHexString(SHA256.HashData(contents)).ToLowerInvariant();
            }
            catch
            {
                // Some proxy or partially parsed material blobs have no serializable payload.
            }

            string sourceIdentity = modelBin.ZipEntryName
                ?? modelBin.FilePath
                ?? modelBin.Name
                ?? "unknown_modelbin";
            return $"fallback-{sourceIdentity}-{materialId:X8}";
        }

        private static JsonObject CreateMaterialJson(
            string key,
            MaterialBlob material,
            ModelBinNode modelBin,
            string materialName,
            uint materialId)
        {
            var bundle = material.Bundle;
            var materialResource = bundle?.Blobs.OfType<MaterialResourceBlob>().FirstOrDefault();
            var shaderParameters = bundle?.Blobs.OfType<MaterialShaderParameterBlob>().FirstOrDefault();

            var colors = new JsonArray();
            var scalars = new JsonArray();
            var settings = new JsonArray();
            var textures = new JsonArray();
            var samplers = new JsonArray();
            var vectors = new JsonArray();

            if (shaderParameters != null)
            {
                foreach (var parameter in shaderParameters.Parameters)
                {
                    var parameterJson = CreateParameterJson(parameter);

                    switch (parameterJson["category"]?.GetValue<string>())
                    {
                        case "color": colors.Add(parameterJson.DeepClone()); break;
                        case "scalar": scalars.Add(parameterJson.DeepClone()); break;
                        case "setting": settings.Add(parameterJson.DeepClone()); break;
                        case "texture": textures.Add(parameterJson.DeepClone()); break;
                        case "sampler": samplers.Add(parameterJson.DeepClone()); break;
                        default: vectors.Add(parameterJson.DeepClone()); break;
                    }
                }
            }

            return new JsonObject
            {
                ["key"] = key,
                ["name"] = materialName,
                ["sourceMaterialId"] = $"0x{materialId:X8}",
                ["materialPath"] = materialResource?.Path ?? string.Empty,
                ["unreal"] = CreateUnrealMaterialHint(materialName, materialResource?.Path),
                ["colors"] = colors,
                ["scalars"] = scalars,
                ["settings"] = settings,
                ["textures"] = textures,
                ["samplers"] = samplers,
                ["vectors"] = vectors
            };
        }

        private static JsonObject CreateParameterJson(ShaderParameter parameter)
        {
            string? knownName = NameHashService.Instance.GetName(parameter.NameHash);
            string name = string.IsNullOrWhiteSpace(knownName)
                ? $"Unknown Hash (0x{parameter.NameHash:X8})"
                : knownName;

            return new JsonObject
            {
                ["name"] = name,
                ["nameHash"] = $"0x{parameter.NameHash:X8}",
                ["type"] = parameter.Type.ToString(),
                ["category"] = GetParameterExportCategory(parameter.Type, name),
                ["guid"] = parameter.Guid == Guid.Empty ? null : parameter.Guid.ToString("D"),
                ["value"] = CreateParameterValueJson(parameter.Value)
            };
        }

        private static JsonObject CreateUnrealMaterialHint(string materialName, string? materialPath)
        {
            string text = $"{materialName} {materialPath}".ToLowerInvariant();
            string family = text.Contains("carpaint") || text.Contains("car_paint")
                ? "carPaint"
                : text.Contains("glass") || text.Contains("windshield") || text.Contains("lens")
                    ? "glass"
                    : text.Contains("decal") || text.Contains("livery") || text.Contains("sticker")
                        ? "decal"
                        : text.Contains("emiss") || text.Contains("light")
                            ? "emissive"
                            : "pbr";

            return new JsonObject
            {
                ["family"] = family,
                ["masterMaterial"] = family == "carPaint" ? "M_Forza_CarPaint" : "M_Forza_PBR",
                ["translation"] = "heuristic",
                ["notes"] = family == "carPaint"
                    ? "Dynamic game paint; use a static clear-coat fallback or set the desired paint color in Unreal."
                    : "Simplified PBR translation; shader graph parity is not implied."
            };
        }

        private static string GetParameterExportCategory(ShaderParameterType type, string name)
        {
            if (type == ShaderParameterType.Texture2D) return "texture";
            if (type == ShaderParameterType.Sampler) return "sampler";
            if (type == ShaderParameterType.Bool) return "setting";
            if (type is ShaderParameterType.Float or ShaderParameterType.Int) return "scalar";
            if (type == ShaderParameterType.Color ||
                name.Contains("color", StringComparison.OrdinalIgnoreCase) ||
                name.Contains("tint", StringComparison.OrdinalIgnoreCase) ||
                name.Contains("f0", StringComparison.OrdinalIgnoreCase))
                return "color";
            return "vector";
        }

        private static JsonNode? CreateParameterValueJson(object? value)
        {
            return value switch
            {
                null => null,
                bool boolValue => JsonValue.Create(boolValue),
                byte byteValue => JsonValue.Create(byteValue),
                short shortValue => JsonValue.Create(shortValue),
                int intValue => JsonValue.Create(intValue),
                uint uintValue => JsonValue.Create(uintValue),
                long longValue => JsonValue.Create(longValue),
                // Some game material constants are uninitialised sentinels (NaN/Infinity).
                // JSON has no representation for them; preserve the parameter while exporting
                // an explicit null instead of failing the whole vehicle package.
                float floatValue => CreateFiniteJsonNumber(floatValue),
                double doubleValue => CreateFiniteJsonNumber(doubleValue),
                string stringValue => JsonValue.Create(stringValue),
                Vector2 vector => new JsonObject
                {
                    ["x"] = CreateFiniteJsonNumber(vector.X),
                    ["y"] = CreateFiniteJsonNumber(vector.Y)
                },
                Vector4 vector => new JsonObject
                {
                    ["x"] = CreateFiniteJsonNumber(vector.X),
                    ["y"] = CreateFiniteJsonNumber(vector.Y),
                    ["z"] = CreateFiniteJsonNumber(vector.Z),
                    ["w"] = CreateFiniteJsonNumber(vector.W)
                },
                TextureParameter texture => new JsonObject
                {
                    ["path"] = texture.Path,
                    ["pathHash"] = $"0x{texture.PathHash:X8}"
                },
                SamplerParameter sampler => new JsonObject
                {
                    ["addressU"] = sampler.AddressU,
                    ["addressV"] = sampler.AddressV,
                    ["filterMode"] = sampler.UnkType
                },
                ColorGradientParameter gradient => new JsonArray(
                    gradient.Values.Select(item => CreateParameterValueJson(item)).ToArray()),
                _ => JsonValue.Create(value.ToString())
            };
        }

        private static JsonNode? CreateFiniteJsonNumber(float value)
            => float.IsFinite(value) ? JsonValue.Create(value) : null;

        private static JsonNode? CreateFiniteJsonNumber(double value)
            => double.IsFinite(value) ? JsonValue.Create(value) : null;

        private static short GetAssignedMaterialIdForExport(MeshBlob meshBlob)
        {
            return meshBlob.MaterialIds != null && meshBlob.MaterialIds.Length > 1
                ? meshBlob.MaterialIds[1]
                : meshBlob.MaterialId;
        }

        private static uint GetMaterialIdForExport(MaterialBlob material)
        {
            var metadata = material.Metadatas
                .OfType<ForzaTools.Bundles.Metadata.IdentifierMetadata>()
                .FirstOrDefault();
            return metadata?.Id ?? material.Id;
        }

        private static string BuildNodePath(IViewerNode node)
        {
            var names = new Stack<string>();
            IViewerNode? current = node;
            while (current != null)
            {
                names.Push(current.Name);
                current = current.Parent;
            }
            return string.Join("/", names);
        }

        private static string BuildMaterialInventoryFileName(IReadOnlyCollection<IViewerNode> selectedNodes)
        {
            string baseName = selectedNodes.Count == 1
                ? selectedNodes.First().Name
                : "selected_vehicle";

            foreach (char invalidCharacter in Path.GetInvalidFileNameChars())
                baseName = baseName.Replace(invalidCharacter, '_');

            baseName = Path.GetFileNameWithoutExtension(baseName);
            return $"{baseName}_materials.json";
        }
    }
}
