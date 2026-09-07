# Vehicle Extraction for Unreal

This fork contains workflow helpers for extracting a static Forza vehicle into a DCC/Unreal pipeline. The target use case is not a fully drivable car with skeletons, animation, damage states, or game behavior. The target is a complete visible vehicle that can be placed in a fixed Unreal Engine scene with usable geometry, material metadata, and PNG textures.

## What Was Added

- `Save All to JSON` in the 3D Viewer toolbar.
- PNG as the default texture export format for model export workflows.
- Better texture path mapping for OBJ/FBX exports.
- A compact material inventory JSON for selected loaded vehicle parts.
- A texture package written next to that JSON.
- Automatic export of vehicle-specific swatchbin textures as PNG.
- Automatic lookup of shared materialbin/shaderbin texture references from the configured game library.
- Filtering for damage/impact textures that are not useful for a clean static vehicle import.
- A neutral fallback description for dynamic car paint materials when no fixed paint texture or color is present.

## Expected Workflow

1. Configure the game install path in ForzaTech Studio so material and texture library lookup can resolve game paths.
2. Open the vehicle archive or car scene in the 3D Viewer.
3. Select the whole vehicle root, a carbin/modelbin root, or the set of visible vehicle parts you want to export.
4. Click `Save All to JSON`.
5. Choose an output path for the material inventory JSON.
6. The tool writes the JSON and creates a sibling texture folder.

For example:

```text
selected_vehicle_materials.json
selected_vehicle_materials Textures/
  Vehicle/
  Library/
```

## Output Contents

The JSON contains the material information needed to rebuild a simplified Unreal material setup:

- selected mesh/part names
- source modelbin names
- assigned material IDs
- material names
- source `materialbin` paths
- color/vector parameters
- scalar parameters
- texture parameter slots
- resolved texture file paths when a texture was found

The texture folder is split by source:

- `Vehicle/` contains swatchbin textures that came directly from the loaded vehicle archive, such as interior textures, gauges, labels, AO maps, and car-specific masks.
- `Library/` contains PNG textures resolved from shared game material/shader library references, such as rubber, plastic, metal, leather, brake, trim, or other reusable material textures.

## Important Material Notes

Not every material has a texture. Some Forza materials are driven mostly by color/vector/scalar parameters, such as base color tint, roughness, metallic, clear coat, or other shader values. In those cases, the exporter keeps the parameter values in JSON instead of inventing a fake PNG.

Car paint is also special. In game, paint can be dynamic and user-selected rather than a single fixed texture. For static Unreal import, treat car paint as a material you will usually recreate manually, using the JSON values when present or a neutral clear-coat fallback when the source does not provide a fixed paint texture.

Normal, AO, roughness, metallic, and mask maps may need manual Unreal material wiring. The exporter gathers the source files and metadata, but it does not yet create Unreal materials automatically.

## What This Does Not Do Yet

- It does not create Unreal Engine materials automatically.
- It does not import assets into Unreal.
- It does not fully translate Forza shader logic into Unreal shader graphs.
- It does not preserve animation, skeleton behavior, physics, damage states, or runtime customization logic.

## Recommended Next Step

The next useful tool would be an Unreal-side importer that reads the material inventory JSON, imports the PNG files, creates basic Unreal materials, assigns texture samples or constant colors by slot, and applies those materials to the imported FBX meshes.

For now, this exporter is meant to make that next step possible by putting the geometry-adjacent material data and all discoverable PNG textures in one predictable package.
