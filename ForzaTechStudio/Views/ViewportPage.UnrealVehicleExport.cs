using ForzaTechStudio.Services;
using ForzaTechStudio.ViewModels.ThreeDViewer;
using Microsoft.UI.Xaml;
using Microsoft.UI.Xaml.Controls;
using System;
using System.Collections.Generic;
using System.IO;
using System.Linq;
using System.Numerics;
using System.Text.Json;
using System.Threading.Tasks;
using Windows.Storage.Pickers;

namespace ForzaTechStudio.Views
{
    public sealed partial class ViewportPage : Page
    {
        private async void ExportVehicleForUnreal_Click(object sender, RoutedEventArgs e)
        {
            try
            {
                await ExportVehicleForUnrealAsync();
            }
            catch (Exception ex)
            {
                App.ShowErrorDialog($"Unreal vehicle export failed: {ex.Message}");
            }
        }

        /// <summary>
        /// The deliberately simple, primary user workflow.  One folder choice produces an FBX,
        /// a v3 material manifest and its PNG package.  This prevents a user from accidentally
        /// exporting geometry and material data from different vehicle selections.
        /// </summary>
        private async Task ExportVehicleForUnrealAsync()
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
                App.ShowErrorDialog("Select the whole vehicle in the scene tree, then click Export Vehicle for Unreal.");
                return;
            }

            var fullModelBins = new HashSet<ModelBinNode>();
            var meshes = new HashSet<MeshNode>();
            foreach (var node in selectedNodes)
                CollectMaterialExportScope(node, fullModelBins, meshes);

            // Forza carries separate, usually low-detail shadow-only geometry. It appears in
            // generic FBX viewers as names such as "Shadow unknown", but is not a visible car
            // part and Unreal generates its own shadows. Keep it out of the normal vehicle hand-off.
            int skippedShadowMeshCount = meshes.RemoveWhere(IsShadowOnlyVehicleMesh);

            if (fullModelBins.Count == 0 || meshes.Count == 0)
            {
                App.ShowErrorDialog("The selection does not contain a complete loaded vehicle. Select the vehicle root in the scene tree.");
                return;
            }

            var document = BuildMaterialInventoryJson(selectedNodes, fullModelBins, meshes);
            if (document["summary"]?["materialCount"]?.GetValue<int>() is not int materialCount || materialCount == 0)
            {
                App.ShowErrorDialog("No vehicle materials were found in the current selection.");
                return;
            }

            var folderPicker = new FolderPicker();
            WinRT.Interop.InitializeWithWindow.Initialize(
                folderPicker,
                WinRT.Interop.WindowNative.GetWindowHandle(App.MainWindow));
            folderPicker.SuggestedStartLocation = PickerLocationId.DocumentsLibrary;
            folderPicker.FileTypeFilter.Add("*");
            var folder = await folderPicker.PickSingleFolderAsync();
            if (folder == null)
                return;

            string baseName = BuildUnrealVehicleExportBaseName(selectedNodes);
            string fbxPath = Path.Combine(folder.Path, $"{baseName}.fbx");
            string jsonPath = Path.Combine(folder.Path, $"{baseName}_materials.json");
            string unrealJsonPath = Path.Combine(folder.Path, $"{baseName}_unreal.json");
            var exportModels = BuildUnrealVehicleExportModels(selectedNodes, fullModelBins, meshes);
            if (exportModels.Count == 0)
            {
                App.ShowErrorDialog("No exportable vehicle meshes were found in the current selection.");
                return;
            }

            IsLoading = true;
            LoadingStatus = "Building Unreal FBX geometry...";
            await Task.Yield();

            try
            {
                // The PNG material package is the authoritative texture source for Unreal. Avoid
                // exporting a second, ambiguous legacy texture folder beside the FBX.
                // Unreal's FBX importer accepts the service's FBX 7.4 ASCII output and
                // preserves its Model -> Geometry hierarchy.  Do not use the custom
                // binary writer here: some FBX SDK consumers reject that stream before
                // scene creation, which previously forced users to flatten the vehicle
                // through OBJ and lose per-part editability.
                await Task.Run(() => FbxExportService.Export(
                    exportModels,
                    fbxPath,
                    FbxExportFormat.Ascii,
                    null,
                    _exportOptions));

                LoadingStatus = "Resolving materials and exporting PNG textures...";
                await Task.Yield();
                var sourceZipPaths = fullModelBins
                    .Concat(meshes.Select(mesh => mesh.ParentModelBin))
                    .Where(modelBin => modelBin != null && !string.IsNullOrWhiteSpace(modelBin.SourceZipPath))
                    .Select(modelBin => modelBin.SourceZipPath!)
                    .Distinct(StringComparer.OrdinalIgnoreCase)
                    .ToList();
                var package = await ExportCompleteVehicleTexturePackageAsync(document, jsonPath, sourceZipPaths);
                await File.WriteAllTextAsync(jsonPath, document.ToJsonString(new JsonSerializerOptions { WriteIndented = true }));
                var unrealDocument = BuildUnrealVehicleManifest(document, Path.GetFileName(jsonPath));
                await File.WriteAllTextAsync(unrealJsonPath, unrealDocument.ToJsonString(new JsonSerializerOptions { WriteIndented = true }));

                await new ContentDialog
                {
                    Title = "Vehicle ready for Unreal",
                    Content = $"Done. Import the FBX into Unreal, then run the Unreal importer script.\n\n" +
                              $"FBX: {fbxPath}\n" +
                              $"Unreal JSON: {unrealJsonPath}\n" +
                              $"PNG textures: {package.PackageDirectory}\n\n" +
                              $"{materialCount} materials, {package.ExportedTextureCount} PNG texture(s), " +
                              $"and {skippedShadowMeshCount} shadow-only mesh(es) skipped.\n\n" +
                              "Use the small Unreal JSON in the Unreal importer. The detailed materials JSON is kept only as a backup.",
                    CloseButtonText = "OK",
                    XamlRoot = XamlRoot
                }.ShowAsync();
            }
            catch (Exception ex)
            {
                await ShowError($"Unreal vehicle export failed.\n\n{ex.Message}");
            }
            finally
            {
                IsLoading = false;
                LoadingStatus = string.Empty;
            }
        }

        private List<ModelBinExportData> BuildUnrealVehicleExportModels(
            IEnumerable<IViewerNode> selectedNodes,
            IEnumerable<ModelBinNode> fullModelBins,
            IEnumerable<MeshNode> meshes)
        {
            var selectedMeshes = meshes.ToHashSet();
            var carbinInstances = EnumerateViewerNodes<CarbinModelNode>(selectedNodes)
                .Where(instance => instance.IsChecked == true && instance.UseTransforms)
                .Select(instance =>
                {
                    var modelBin = FindMatchingModelBin(instance);
                    return modelBin != null &&
                           TryResolveCarbinInstanceTransform(instance, modelBin, out Matrix4x4 transform)
                        ? (Instance: instance, ModelBin: modelBin, Transform: transform, IsValid: true)
                        : (Instance: instance, ModelBin: modelBin!, Transform: Matrix4x4.Identity, IsValid: false);
                })
                .Where(item => item.IsValid)
                .OrderBy(item => BuildNodePath(item.Instance), StringComparer.OrdinalIgnoreCase)
                .ToList();

            // Modelbins used by a Carbin instance are source libraries. Exporting their raw
            // meshes as well would leave a duplicate wheel/trim/etc. at the vehicle origin.
            var instancedModelBins = carbinInstances
                .Select(item => item.ModelBin)
                .ToHashSet();

            var result = fullModelBins
                .Where(model => !instancedModelBins.Contains(model))
                .OrderBy(model => model.Name, StringComparer.OrdinalIgnoreCase)
                .Select(model => new ModelBinExportData(
                    GetUnrealExportModelIdentity(model),
                    model.Bundle,
                    model.Children.OfType<MeshNode>()
                        .Where(selectedMeshes.Contains)
                        .Where(mesh => !IsShadowOnlyVehicleMesh(mesh))
                        .Where(mesh => mesh.GeometryData != null &&
                                       _exportOptions.ShouldExportMesh(mesh.Name, mesh.GeometryData.SourceMesh))
                        .Select(mesh => (Name: mesh.Name, Data: mesh.GeometryData!))
                        .ToList()))
                .Where(model => model.Meshes.Count > 0)
                .ToList();

            int instanceOrdinal = 0;
            foreach (var item in carbinInstances)
            {
                string instanceName = BuildUnrealInstanceName(item.Instance, instanceOrdinal++);
                var instanceMeshes = item.ModelBin.Children
                    .OfType<MeshNode>()
                    .Where(mesh => mesh.IsChecked == true)
                    .Where(mesh => !IsShadowOnlyVehicleMesh(mesh))
                    .Where(mesh => mesh.GeometryData != null &&
                                   _exportOptions.ShouldExportMesh(mesh.Name, mesh.GeometryData.SourceMesh))
                    .Select(mesh => (
                        Name: $"{instanceName}__{mesh.Name}",
                        Data: CreateCarbinExportGeometry(mesh.GeometryData!, item.Transform)))
                    .ToList();

                if (instanceMeshes.Count > 0)
                    result.Add(new ModelBinExportData(
                        GetUnrealExportModelIdentity(item.ModelBin),
                        item.ModelBin.Bundle,
                        instanceMeshes));
            }

            return result;
        }

        private static string BuildUnrealInstanceName(CarbinModelNode instance, int ordinal)
        {
            string source = $"{BuildNodePath(instance)}|{instance.ModelIndex}|{ordinal}";
            uint hash = 2166136261;
            foreach (char character in source)
            {
                hash ^= char.ToUpperInvariant(character);
                hash *= 16777619;
            }

            string label = string.IsNullOrWhiteSpace(instance.PartName) ? instance.Name : instance.PartName;
            return $"{label}_{instance.ModelIndex:D2}_{hash:X8}";
        }

        private static ForzaGeometryData CreateCarbinExportGeometry(
            ForzaGeometryData source,
            Matrix4x4 instanceTransform)
        {
            // Keep the original quantized positions and MeshBlob so FBX material-slot names
            // remain identical to the JSON manifest. ResolveGeometry will apply this combined
            // transform once, baking the instance at its real vehicle position.
            Matrix4x4 sourceTransform = IsFiniteMatrix(source.BoneTransform)
                ? source.BoneTransform
                : Matrix4x4.Identity;
            Matrix4x4 combinedTransform = sourceTransform * instanceTransform;
            if (!IsFiniteMatrix(combinedTransform))
                combinedTransform = sourceTransform;

            return new ForzaGeometryData
            {
                Name = source.Name,
                MaterialName = source.MaterialName,
                Positions = source.Positions,
                Normals = source.Normals,
                UVs = source.UVs,
                UvChannels = source.UvChannels,
                Colors = source.Colors,
                Indices = source.Indices,
                InitialRenderPositions = source.InitialRenderPositions,
                SourceMesh = source.SourceMesh,
                RawPositions = source.RawPositions,
                MinVertexIndex = source.MinVertexIndex,
                BoneTransform = combinedTransform,
                BoneIndex = source.BoneIndex,
                BoneName = source.BoneName,
                OriginalBoneTransform = source.OriginalBoneTransform,
                SourceBone = source.SourceBone,
                OriginalMeshTranslateRelativeToBone = source.OriginalMeshTranslateRelativeToBone,
                DamageRawPositions = source.DamageRawPositions,
                RotationEulerDegrees = source.RotationEulerDegrees
            };
        }

        private static string GetUnrealExportModelIdentity(ModelBinNode model) =>
            model.ZipEntryName ?? model.FilePath ?? model.Name ?? model.FileName ?? "vehicle";

        private static bool IsShadowOnlyVehicleMesh(MeshNode mesh) =>
            mesh.IsShadow || mesh.Name.StartsWith("Shadow", StringComparison.OrdinalIgnoreCase);

        private static string BuildUnrealVehicleExportBaseName(IReadOnlyCollection<IViewerNode> selectedNodes)
        {
            string name = selectedNodes.Count == 1 ? selectedNodes.First().Name : "vehicle";
            name = Path.GetFileNameWithoutExtension(name);
            foreach (char invalid in Path.GetInvalidFileNameChars())
                name = name.Replace(invalid, '_');
            return string.IsNullOrWhiteSpace(name) ? "vehicle" : name;
        }
    }
}
