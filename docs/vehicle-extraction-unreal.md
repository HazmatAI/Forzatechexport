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
3. Select the **whole vehicle root** in the scene tree.
4. Click the blue **Export Vehicle for Unreal** button. This is the normal workflow.
5. Choose one output folder. The app writes the FBX, JSON, and PNG texture package together.

`Save All to JSON` remains available as an advanced JSON-only diagnostic export. It
does not write geometry and should not be used for the normal Unreal workflow.

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

Schema version 3 also makes the material assignment machine-readable rather than
relying on a human reading mesh names:

- `parts[].unrealMaterialSlot` is the exact sanitized, collision-resistant
  material-slot name emitted by the current OBJ/FBX exporters. It combines the
  friendly material name, source modelbin, and active material ID; an Unreal-side
  tool should assign against this rather than the generic Forza material name.
- `parts[].materialBinding` retains the active Forza material ID, material groups,
  and the group/index used by the viewer.
- `parts[].renderHints` exposes transparent/decal/alpha-to-coverage and LOD hints.
- Every parameter records `nameHash`, type, category, value and (when present) GUID.
- When the game library is configured, `shader` records the resolved materialbin →
  shaderbin chain plus the CBMP/TXMP/SPMP mapping tables, and `effectiveParameters`
  records the winning value with its source: `shaderDefault`, `materialbinOverride`,
  or `vehicleOverride`.
- `resolvedTextures[].unrealBinding` is a deliberately conservative semantic hint
  (`baseColor`, `normal`, `packedOrm`, `roughness`, `metallic`, `ambientOcclusion`,
  `emissive`, `opacity`, or `custom`), including expected colour space/compression
  and packed-map channels. It is a heuristic, not a reverse-engineered Forza shader.
- `unreal.family` identifies the intended starting family (`pbr`, `carPaint`,
  `glass`, `decal`, or `emissive`). Car paint remains a static Unreal approximation.

The texture folder is split by source:

- `Vehicle/` contains swatchbin textures that came directly from the loaded vehicle archive, such as interior textures, gauges, labels, AO maps, and car-specific masks.
- `Library/` contains PNG textures resolved from shared game material/shader library references, such as rubber, plastic, metal, leather, brake, trim, or other reusable material textures.

## Important Material Notes

Not every material has a texture. Some Forza materials are driven mostly by color/vector/scalar parameters, such as base color tint, roughness, metallic, clear coat, or other shader values. In those cases, the exporter keeps the parameter values in JSON instead of inventing a fake PNG.

Car paint is also special. In game, paint can be dynamic and user-selected rather than a single fixed texture. For static Unreal import, treat car paint as a material you will usually recreate manually, using the JSON values when present or a neutral clear-coat fallback when the source does not provide a fixed paint texture.

Normal, AO, roughness, metallic, and mask maps may need manual Unreal material wiring. The exporter gathers the source files and metadata, and the supplied Unreal Python script creates a simple starting graph; it does not reproduce proprietary shader logic.

## Unreal Importer (Schema v3)

The repository includes `Tools/Unreal/ForzaMaterialImporter.py`. It is an Unreal
Editor Python script, not a command-line Python program. It imports every exported
PNG, marks normal and mask textures with appropriate Unreal import settings, creates
one editable simplified PBR material for every inventory material, and assigns those
materials to already-imported static-mesh slots by `unrealMaterialSlot`.

1. Export the vehicle geometry as FBX and the material inventory from the **same
   selection** in the 3D Viewer.
2. Import the FBX into an Unreal content folder, preserving material-slot names.
3. In Unreal, enable the Python Editor Script Plugin. Open **Output Log → Python**
   and run:

   ```python
   exec(open(r"D:\path\to\ForzaMaterialImporter.py", encoding="utf-8").read())
   import_forza_vehicle(
       r"D:\path\to\selected_vehicle_materials.json",
       "/Game/ForzaVehicle",
       ["/Game/ForzaVehicle/Meshes"])
   ```

4. Inspect the generated `/Game/ForzaVehicle/Materials` assets. `carPaint` uses a
   neutral clear static fallback when no fixed paint colour/texture is known. A
   warning for an ambiguous slot means that distinct Forza material instances were
   collapsed to the same FBX slot name; use the JSON's `parts` list to resolve that
   one slot manually.

The importer accepts legacy schema v2 inventories, but only v3 exports include the
full effective-parameter provenance and semantic texture hints.

## What This Does Not Do Yet

- It does not fully translate Forza shader logic into Unreal shader graphs.
- It does not preserve animation, skeleton behavior, physics, damage states, or runtime customization logic.

## Recommended Next Step

The next useful tool would be an Unreal-side importer that reads the material inventory JSON, imports the PNG files, creates basic Unreal materials, assigns texture samples or constant colors by slot, and applies those materials to the imported FBX meshes.

For now, this exporter is meant to make that next step possible by putting the geometry-adjacent material data and all discoverable PNG textures in one predictable package.
