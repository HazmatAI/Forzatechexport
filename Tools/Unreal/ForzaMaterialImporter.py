"""Create simplified Unreal materials from a ForzaTech Studio material inventory.

Run this *inside the Unreal Editor Python console* after importing the FBX exported
by ForzaTech Studio:

    exec(open(r"D:\\...\\ForzaMaterialImporter.py", encoding="utf-8").read())
    import_forza_vehicle(r"D:\\...\\selected_vehicle_unreal.json",
                         "/Game/ForzaVehicle",
                         ["/Game/ForzaVehicle/Meshes"])

The importer deliberately builds one simple PBR material per Forza material instance.
It does not claim to reproduce the proprietary Forza shader graphs.  Its purpose is
to create a useful, editable visual baseline and to preserve every decision in the
inventory JSON for subsequent refinement.
"""

import json
import os
import re

import unreal


def _safe_name(value, fallback="Unnamed"):
    value = re.sub(r"[^A-Za-z0-9_]", "_", str(value or ""))
    value = re.sub(r"_+", "_", value).strip("_")
    return value[:80] or fallback


def _log(message):
    unreal.log("[ForzaMaterialImporter] {}".format(message))


def _warn(message):
    unreal.log_warning("[ForzaMaterialImporter] {}".format(message))


def _asset_tools():
    return unreal.AssetToolsHelpers.get_asset_tools()


def _import_texture(source_file, destination_path):
    task = unreal.AssetImportTask()
    task.filename = source_file
    task.destination_path = destination_path
    task.automated = True
    task.replace_existing = True
    task.save = True
    _asset_tools().import_asset_tasks([task])
    return task.imported_object_paths[0] if task.imported_object_paths else None


def _import_package_textures(inventory_path, document, destination_root):
    """Import the already-exported PNGs and return JSON-relative path -> Texture."""
    package_name = document.get("texturePackage", {}).get("folder")
    if not package_name:
        package_name = os.path.splitext(os.path.basename(inventory_path))[0] + " Textures"
    package_root = os.path.join(os.path.dirname(inventory_path), package_name)
    result = {}
    if not os.path.isdir(package_root):
        _warn("Texture package not found: {}. Materials will use constants where possible.".format(package_root))
        return result

    for root, _, file_names in os.walk(package_root):
        for file_name in file_names:
            if not file_name.lower().endswith(".png"):
                continue
            source = os.path.join(root, file_name)
            relative = os.path.relpath(source, os.path.dirname(inventory_path)).replace("\\", "/")
            folder = os.path.relpath(root, package_root).replace("\\", "/")
            destination = destination_root + "/Textures"
            if folder != ".":
                destination += "/" + "/".join(_safe_name(part) for part in folder.split("/"))
            imported_path = _import_texture(source, destination)
            if imported_path:
                texture = unreal.load_asset(imported_path)
                result[relative] = texture
    _log("Imported {} PNG texture(s).".format(len(result)))
    return result


def _set_texture_settings(texture, binding):
    if not texture or not binding:
        return
    try:
        texture.set_editor_property("srgb", bool(binding.get("sRGB", False)))
        compression = binding.get("compression")
        if compression == "normalmap":
            texture.set_editor_property("compression_settings", unreal.TextureCompressionSettings.TC_NORMALMAP)
        elif compression == "masks":
            texture.set_editor_property("compression_settings", unreal.TextureCompressionSettings.TC_MASKS)
        texture.post_edit_change()
    except Exception as error:
        _warn("Could not apply texture settings to {}: {}".format(texture.get_name(), error))


def _effective_parameters(material):
    parameters = material.get("effectiveParameters")
    if parameters:
        return parameters
    # Schema v2 compatibility: values are still useful, but lack inherited shader defaults.
    flattened = []
    for category in ("colors", "scalars", "settings", "textures", "samplers", "vectors"):
        flattened.extend(material.get(category, []))
    return flattened


def _parameter_value(material, tokens, default=None):
    for parameter in _effective_parameters(material):
        name = parameter.get("name", "").lower()
        if any(token in name for token in tokens):
            return parameter.get("value", default)
    return default


def _linear_color(value, default):
    if isinstance(value, dict):
        return unreal.LinearColor(float(value.get("x", default[0])), float(value.get("y", default[1])),
                                 float(value.get("z", default[2])), float(value.get("w", default[3])))
    if isinstance(value, (list, tuple)) and len(value) >= 3:
        return unreal.LinearColor(float(value[0]), float(value[1]), float(value[2]),
                                 float(value[3]) if len(value) > 3 else default[3])
    return unreal.LinearColor(*default)


def _scalar(value, default):
    return float(value) if isinstance(value, (int, float)) else float(default)


def _texture_bindings(material):
    bindings = []
    for texture in material.get("resolvedTextures", []):
        binding = texture.get("unrealBinding") or {}
        if texture.get("resolved") and texture.get("file"):
            bindings.append((binding.get("role", "custom"), texture, binding))
    return bindings


def _first_binding(bindings, role):
    return next(((texture, binding) for current_role, texture, binding in bindings if current_role == role), None)


def _add_constant(material, value, property_name):
    expression = unreal.MaterialEditingLibrary.create_material_expression(
        material, unreal.MaterialExpressionConstant, -400, len(material.get_editor_property("expressions")) * 120)
    expression.set_editor_property("r", float(value))
    unreal.MaterialEditingLibrary.connect_material_property(expression, "R", property_name)


def _add_color_constant(material, color):
    expression = unreal.MaterialEditingLibrary.create_material_expression(
        material, unreal.MaterialExpressionConstant3Vector, -600, 0)
    expression.set_editor_property("constant", color)
    unreal.MaterialEditingLibrary.connect_material_property(expression, "", unreal.MaterialProperty.MP_BASE_COLOR)


def _add_texture(material, texture, binding, property_name, output="RGB"):
    expression = unreal.MaterialEditingLibrary.create_material_expression(
        material, unreal.MaterialExpressionTextureSample, -300,
        len(material.get_editor_property("expressions")) * 170)
    expression.set_editor_property("texture", texture)
    if binding.get("role") == "normal":
        expression.set_editor_property("sampler_type", unreal.MaterialSamplerType.SAMPLERTYPE_NORMAL)
    unreal.MaterialEditingLibrary.connect_material_property(expression, output, property_name)
    return expression


def _material_family(material):
    exported = material.get("unreal", {}).get("family")
    if exported:
        return exported
    value = (material.get("name", "") + " " + material.get("materialPath", "")).lower()
    if "carpaint" in value or "car_paint" in value:
        return "carPaint"
    if any(token in value for token in ("glass", "windshield", "lens")):
        return "glass"
    if any(token in value for token in ("decal", "livery", "sticker")):
        return "decal"
    return "pbr"


def _build_material(material_data, textures, destination_root):
    material_id = material_data.get("id", "mat")
    asset_name = _safe_name("M_{}_{}".format(material_id, material_data.get("name", "material")))
    package_path = destination_root + "/Materials"
    material = unreal.load_asset(package_path + "/" + asset_name)
    if not material:
        material = _asset_tools().create_asset(asset_name, package_path, unreal.Material, unreal.MaterialFactoryNew())
    if not material:
        raise RuntimeError("Could not create material {}".format(asset_name))

    try:
        unreal.MaterialEditingLibrary.delete_all_material_expressions(material)
    except Exception:
        # Newer editor versions may not expose deletion; re-running remains safe but appends nodes.
        _warn("Could not clear {} before rebuilding; delete it once if this is a re-run.".format(asset_name))

    family = _material_family(material_data)
    if family == "glass":
        material.set_editor_property("blend_mode", unreal.BlendMode.BLEND_TRANSLUCENT)
    else:
        material.set_editor_property("blend_mode", unreal.BlendMode.BLEND_OPAQUE)

    bindings = _texture_bindings(material_data)
    for _, texture_data, binding in bindings:
        texture = textures.get(texture_data.get("file"))
        _set_texture_settings(texture, binding)

    base = _first_binding(bindings, "baseColor")
    if base and textures.get(base[0].get("file")):
        base_expression = _add_texture(material, textures[base[0]["file"]], base[1], unreal.MaterialProperty.MP_BASE_COLOR)
        if "opacityMask" in base[1].get("channels", ""):
            material.set_editor_property("blend_mode", unreal.BlendMode.BLEND_MASKED)
            unreal.MaterialEditingLibrary.connect_material_property(
                base_expression, "A", unreal.MaterialProperty.MP_OPACITY_MASK)
    else:
        fallback = material_data.get("neutralFallback", {}).get("baseColorLinear")
        color = _linear_color(_parameter_value(material_data, ("basecolor", "base_color", "diffuse", "tint", "colour", "color"), fallback),
                              (0.18, 0.18, 0.18, 1.0) if family == "carPaint" else (0.5, 0.5, 0.5, 1.0))
        _add_color_constant(material, color)

    normal = _first_binding(bindings, "normal")
    if normal and textures.get(normal[0].get("file")):
        _add_texture(material, textures[normal[0]["file"]], normal[1], unreal.MaterialProperty.MP_NORMAL)

    packed = _first_binding(bindings, "packedOrm")
    if packed and textures.get(packed[0].get("file")):
        packed_expression = _add_texture(material, textures[packed[0]["file"]], packed[1], unreal.MaterialProperty.MP_AMBIENT_OCCLUSION, "R")
        unreal.MaterialEditingLibrary.connect_material_property(packed_expression, "G", unreal.MaterialProperty.MP_ROUGHNESS)
        unreal.MaterialEditingLibrary.connect_material_property(packed_expression, "B", unreal.MaterialProperty.MP_METALLIC)
    else:
        for role, property_name, tokens, default in (
            ("roughness", unreal.MaterialProperty.MP_ROUGHNESS, ("roughness",), 0.5),
            ("metallic", unreal.MaterialProperty.MP_METALLIC, ("metallic", "metalness"), 0.0),
            ("ambientOcclusion", unreal.MaterialProperty.MP_AMBIENT_OCCLUSION, ("ambientocclusion", "_ao"), 1.0),
        ):
            entry = _first_binding(bindings, role)
            if entry and textures.get(entry[0].get("file")):
                _add_texture(material, textures[entry[0]["file"]], entry[1], property_name, "R")
            else:
                _add_constant(material, _scalar(_parameter_value(material_data, tokens), default), property_name)

    emissive = _first_binding(bindings, "emissive")
    if emissive and textures.get(emissive[0].get("file")):
        _add_texture(material, textures[emissive[0]["file"]], emissive[1], unreal.MaterialProperty.MP_EMISSIVE_COLOR)

    opacity = _first_binding(bindings, "opacity")
    if family == "glass":
        if opacity and textures.get(opacity[0].get("file")):
            _add_texture(material, textures[opacity[0]["file"]], opacity[1], unreal.MaterialProperty.MP_OPACITY, "A")
        else:
            _add_constant(material, _scalar(_parameter_value(material_data, ("opacity", "alpha")), 0.35), unreal.MaterialProperty.MP_OPACITY)

    unreal.MaterialEditingLibrary.recompile_material(material)
    unreal.EditorAssetLibrary.save_loaded_asset(material)
    return material


def _assign_materials(document, built_materials, mesh_paths):
    """Assign by the exact slot name emitted by the ForzaTech OBJ/FBX exporters."""
    slot_to_ids = {}
    for part in document.get("parts", []):
        slot = part.get("unrealMaterialSlot")
        if not slot:
            # Schema v2 only recorded the display mesh name. The Viewer normally
            # appends the source material in brackets, e.g. "body_LOD0 [chrome]".
            legacy_match = re.search(r"\[([^\]]+)\]\s*$", part.get("mesh", ""))
            slot = legacy_match.group(1) if legacy_match else part.get("mesh", "")
        material_id = part.get("material")
        if slot and material_id in built_materials:
            slot_to_ids.setdefault(slot.lower(), set()).add(material_id)

    for slot, ids in slot_to_ids.items():
        if len(ids) > 1:
            _warn("Slot '{}' has {} material instances; it cannot be assigned unambiguously.".format(slot, len(ids)))

    registry = unreal.AssetRegistryHelpers.get_asset_registry()
    assigned = 0
    for mesh_path in mesh_paths or []:
        for asset_data in registry.get_assets_by_path(mesh_path, recursive=True):
            static_mesh = asset_data.get_asset()
            if not isinstance(static_mesh, unreal.StaticMesh):
                continue
            for index, static_material in enumerate(static_mesh.get_editor_property("static_materials")):
                slot_name = str(static_material.get_editor_property("material_slot_name")).lower()
                ids = slot_to_ids.get(slot_name, set())
                if len(ids) != 1:
                    continue
                material = built_materials[next(iter(ids))]
                try:
                    static_mesh.set_material(index, material)
                    assigned += 1
                except Exception as error:
                    _warn("Could not assign {}[{}]: {}".format(static_mesh.get_name(), index, error))
            unreal.EditorAssetLibrary.save_loaded_asset(static_mesh)
    _log("Assigned {} static-mesh material slot(s).".format(assigned))


def import_forza_vehicle(inventory_path, destination_root="/Game/ForzaVehicle", mesh_paths=None):
    """Import PNGs, build PBR materials, and assign material slots on imported static meshes."""
    inventory_path = os.path.abspath(inventory_path)
    with open(inventory_path, "r", encoding="utf-8") as source:
        document = json.load(source)
    supported_schemas = ("ForzaTechStudio.UnrealVehicle", "ForzaTechStudio.MaterialInventory")
    if document.get("schema") not in supported_schemas:
        raise ValueError("Not a ForzaTech Studio Unreal vehicle manifest: {}".format(inventory_path))

    _log("Reading schema v{} with {} material(s).".format(document.get("schemaVersion", 2), len(document.get("materials", []))))
    textures = _import_package_textures(inventory_path, document, destination_root)
    built = {}
    for material in document.get("materials", []):
        try:
            built[material["id"]] = _build_material(material, textures, destination_root)
        except Exception as error:
            _warn("Skipped material {}: {}".format(material.get("id", "unknown"), error))
    _assign_materials(document, built, mesh_paths)
    _log("Finished: {} Unreal material(s) created.".format(len(built)))
    return built
