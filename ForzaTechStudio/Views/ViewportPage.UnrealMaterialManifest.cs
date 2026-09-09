using System;
using System.Collections.Generic;
using System.Linq;
using System.Text.Json.Nodes;

namespace ForzaTechStudio.Views
{
    public sealed partial class ViewportPage
    {
        /// <summary>
        /// Creates the deliberately small manifest consumed by the Unreal Python importer.
        /// The full material inventory remains an optional forensic/debug backup; this file only
        /// retains data that changes the visible Unreal result or identifies an FBX material slot.
        /// </summary>
        private static JsonObject BuildUnrealVehicleManifest(JsonObject inventory, string sourceInventoryFileName)
        {
            var parts = inventory["parts"] as JsonArray ?? [];
            var usedIds = parts
                .OfType<JsonObject>()
                .Select(part => part["material"]?.GetValue<string>())
                .Where(id => !string.IsNullOrWhiteSpace(id))
                .Cast<string>()
                .ToHashSet(StringComparer.OrdinalIgnoreCase);

            var compactParts = new JsonArray(parts
                .OfType<JsonObject>()
                .Select(part => (JsonNode?)new JsonObject
                {
                    ["unrealMaterialSlot"] = part["unrealMaterialSlot"]?.DeepClone(),
                    ["material"] = part["material"]?.DeepClone()
                })
                .ToArray());

            var compactMaterials = new JsonArray((inventory["materials"] as JsonArray ?? [])
                .OfType<JsonObject>()
                .Where(material => usedIds.Contains(material["id"]?.GetValue<string>() ?? string.Empty))
                .Select(CreateCompactUnrealMaterial)
                .Cast<JsonNode?>()
                .ToArray());

            return new JsonObject
            {
                ["schema"] = "ForzaTechStudio.UnrealVehicle",
                ["schemaVersion"] = 1,
                ["sourceInventory"] = sourceInventoryFileName,
                ["summary"] = new JsonObject
                {
                    ["partCount"] = compactParts.Count,
                    ["materialCount"] = compactMaterials.Count
                },
                ["texturePackage"] = inventory["texturePackage"]?.DeepClone(),
                ["parts"] = compactParts,
                ["materials"] = compactMaterials
            };
        }

        private static JsonObject CreateCompactUnrealMaterial(JsonObject material)
        {
            // Keep only render-relevant values. Name hashes, GUIDs, samplers, shader defaults and
            // unresolved internal Forza structures are intentionally excluded from this hand-off.
            var result = new JsonObject
            {
                ["id"] = material["id"]?.DeepClone(),
                ["name"] = material["name"]?.DeepClone(),
                // The full inventory duplicates these bindings below Unreal's material hint.
                // The importer only needs the family and the bindings retained in resolvedTextures.
                ["unreal"] = new JsonObject
                {
                    ["family"] = material["unreal"]?["family"]?.DeepClone(),
                    ["masterMaterial"] = material["unreal"]?["masterMaterial"]?.DeepClone()
                },
                ["resolvedTextures"] = new JsonArray(GetUnrealRelevantTextures(material["resolvedTextures"] as JsonArray)
                    .Select(texture => (JsonNode?)new JsonObject
                    {
                        ["slot"] = texture["slot"]?.DeepClone(),
                        ["file"] = texture["file"]?.DeepClone(),
                        ["resolved"] = texture["resolved"]?.DeepClone(),
                        ["unresolvedReference"] = texture["unresolvedReference"]?.DeepClone(),
                        ["unrealBinding"] = texture["unrealBinding"]?.DeepClone()
                    })
                    .ToArray()),
                ["colors"] = CompactConstants(material["colors"] as JsonArray),
                ["scalars"] = CompactConstants(material["scalars"] as JsonArray),
                ["neutralFallback"] = material["neutralFallback"]?.DeepClone()
            };
            return result;
        }

        private static IEnumerable<JsonObject> GetUnrealRelevantTextures(JsonArray? textures)
        {
            // The Python importer deliberately reads one texture per one of these PBR roles.
            // Forza's snow/mud and other dynamic-effect inputs were previously retained even
            // though the importer never consumed them, which made the hand-off JSON enormous.
            var supportedRoles = new HashSet<string>(StringComparer.OrdinalIgnoreCase)
            {
                "baseColor", "normal", "packedOrm", "roughness", "metallic",
                "ambientOcclusion", "emissive", "opacity"
            };

            return (textures ?? [])
                .OfType<JsonObject>()
                .Where(texture => texture["unrealBinding"]?["role"]?.GetValue<string>() is string role &&
                                  supportedRoles.Contains(role))
                .GroupBy(texture => texture["unrealBinding"]?["role"]?.GetValue<string>(), StringComparer.OrdinalIgnoreCase)
                .Select(group => group.First());
        }

        private static JsonArray CompactConstants(JsonArray? parameters) => new((parameters ?? [])
            .OfType<JsonObject>()
            .Select(parameter => (JsonNode?)new JsonObject
            {
                ["name"] = parameter["name"]?.DeepClone(),
                ["value"] = parameter["value"]?.DeepClone()
            })
            .ToArray());
    }
}
