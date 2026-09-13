# Unreal vehicle pipeline

This repository contains a static-vehicle workflow for moving a selected Forza
vehicle from ForzaTech Studio into Unreal Engine. It is intended for a fixed
scene and visual editing; it does not create a drivable vehicle, skeleton,
physics asset, animation system, or damage system.

## What the workflow produces

The blue **Export Vehicle for Unreal** action writes one export folder containing:

```text
vehicle.fbx
vehicle_unreal.json
vehicle_materials Textures/
  Vehicle/
  Library/
```

The FBX exporter keeps individual geometry pieces and their material-slot names.
Checked Carbin instances are exported with their resolved transforms baked into
the geometry. Source modelbins used only as instance libraries are excluded, so
duplicate wheels or trim are not left at the vehicle origin. Shadow-only and
unknown meshes are filtered by the exporter.

## Export from ForzaTech Studio

1. Configure the game/library paths in **Settings → Game Setup**.
2. Open the vehicle in the 3D Viewer and select the vehicle root (or the exact
   set of parts to export).
3. Click **Export Vehicle for Unreal** and choose an empty output folder.
4. Keep the FBX, `vehicle_unreal.json`, and PNG folder together. The JSON and
   FBX must come from the same export selection.

The exporter uses ASCII FBX output because it is accepted consistently by Unreal
FBX importers while preserving the model/geometry hierarchy. The JSON manifest
contains the sanitized slot name → material ID mapping and resolved texture
metadata.

## Import the meshes in Unreal

Enable **Python Editor Script Plugin** and make sure the Unreal MCP server is
running when using the automated workflow:

```text
ModelContextProtocol.StartServer 8000
```

Import the FBX into a clean folder, for example
`/Game/ForzaImports/F12dtf_Edit2/Meshes`, with these options:

- **Combine Meshes:** disabled
- **Import Materials:** disabled
- **Import Textures:** disabled

Disabling mesh combination is important: every editable geometry piece remains
its own Static Mesh asset and retains its slot name.

## Rebuild materials with Python

`Tools/Unreal/ForzaMaterialImporter.py` is an **Unreal Editor Python script**;
it is not a normal command-line Python module. Open **Output Log**, switch the
command type to **Cmd**, and run:

```text
py exec(open(r"D:\path\to\ForzaMaterialImporter.py", encoding="utf-8").read()); import_forza_vehicle_instances(
    r"D:\path\to\vehicle_unreal.json",
    "/Game/ForzaImports/F12dtf_Edit2",
    ["/Game/ForzaImports/F12dtf_Edit2/Meshes"],
    "/Game/ForzaImports/F12dtf_Edit1")
```

The optional fourth argument is an existing Unreal texture root. The importer
tries to reuse matching textures before importing PNGs into the new destination.
It then:

- creates nine editable master material families under `MasterMaterials`;
- creates one material instance per JSON material under `MaterialInstances`;
- applies conservative base-color, normal, packed ORM, opacity, and emissive
  parameters;
- assigns instances to every matching FBX material slot;
- saves imported textures, masters, instances, and mesh assets.

The workflow log reports texture reuse/import/missing counts, assigned slot count,
and the final master/instance count. A missing texture count of zero means every
resolved PNG reference was found; a few unresolved optional source references may
still exist in the JSON manifest.

## Group the car in a level

After import, place the Static Mesh actors in the target level and create an
empty parent Actor. Attach each part's `StaticMeshComponent` to the parent's
`DefaultSceneRoot` after setting the components to **Movable**. Set the child
scale to the FBX unit conversion required by the export (the tested workflow
uses `100,100,100`) and keep the parent at scale `1,1,1`.

Use clear labels such as `F12dtf_Edit2_Root` and an Outliner folder such as
`ForzaImports/F12dtf_Edit2`. Moving the root then moves the complete car while
each piece remains independently selectable and editable.

## Verification checklist

- the target level is the expected `/Game/.../Cars` map;
- Combine Meshes was disabled;
- the root has actual component attachments (not just a folder label);
- no `Shadow`/`unknown` meshes were imported;
- every imported slot has a material instance or an intentional fallback;
- the Output Log reports zero missing resolved textures;
- save the level and confirm Unreal shows **All Saved**.

