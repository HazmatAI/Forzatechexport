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


_expression_rows = {}
_primary_carpaint_color = None


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
        # UE 5.8 no longer exposes UObject.post_edit_change() to Python for
        # Texture2D assets. Setting the editor properties dirties the asset;
        # saving it is sufficient to persist the import settings.
        unreal.EditorAssetLibrary.save_loaded_asset(texture)
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


def _configure_document_defaults(document):
    """Capture vehicle-wide defaults needed by simplified shader families."""
    global _primary_carpaint_color
    _primary_carpaint_color = None
    for material in document.get("materials", []):
        if _material_family(material) != "carPaint":
            continue
        value = _parameter_value(
            material,
            ("basecolor", "base_color", "diffuse", "tint", "colour", "color"),
        )
        if isinstance(value, (dict, list, tuple)):
            _primary_carpaint_color = value
            break


def _texture_bindings(material):
    bindings = []
    for texture in material.get("resolvedTextures", []):
        binding = texture.get("unrealBinding") or {}
        if texture.get("resolved") and texture.get("file"):
            bindings.append((binding.get("role", "custom"), texture, binding))
    return bindings


def _first_binding(bindings, role):
    return next(((texture, binding) for current_role, texture, binding in bindings if current_role == role), None)


def _reset_expression_rows(material):
    _expression_rows[material.get_path_name()] = 0


def _next_expression_y(material):
    """Allocate graph rows without reading Material.Expressions (protected in UE 5.8)."""
    path = material.get_path_name()
    row = _expression_rows.get(path, 0)
    _expression_rows[path] = row + 1
    return row * 170


def _add_constant(material, value, property_name):
    expression = unreal.MaterialEditingLibrary.create_material_expression(
        material, unreal.MaterialExpressionConstant, -400, _next_expression_y(material))
    # Every scalar connected by this importer is a normalized PBR input.  Some
    # source parameters are shifts (for example roughness_shift = -0.85), so
    # clamp them instead of emitting invalid material values.
    expression.set_editor_property("r", max(0.0, min(1.0, float(value))))
    unreal.MaterialEditingLibrary.connect_material_property(expression, "R", property_name)


def _add_color_constant(material, color):
    expression = unreal.MaterialEditingLibrary.create_material_expression(
        material, unreal.MaterialExpressionConstant3Vector, -600, _next_expression_y(material))
    expression.set_editor_property("constant", color)
    unreal.MaterialEditingLibrary.connect_material_property(expression, "", unreal.MaterialProperty.MP_BASE_COLOR)


def _add_texture(material, texture, binding, property_name, output="RGB"):
    expression = unreal.MaterialEditingLibrary.create_material_expression(
        material, unreal.MaterialExpressionTextureSample, -300, _next_expression_y(material))
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


def _master_family(material):
    """Return a small, artist-friendly master family for a Forza material."""
    family = _material_family(material)
    if family != "pbr":
        return family
    value = (material.get("name", "") + " " + material.get("materialPath", "")).lower()
    if any(token in value for token in ("tire", "tyre", "rubber")):
        return "tire"
    if "carbon" in value:
        return "carbon"
    if any(token in value for token in ("chrome", "metal", "alum", "steel", "iron", "silver")):
        return "metal"
    if "plastic" in value:
        return "plastic"
    return "pbr"


def _parameter_expression(material, expression_type, name, default, x, y, group):
    expression = unreal.MaterialEditingLibrary.create_material_expression(material, expression_type, x, y)
    expression.set_editor_property("parameter_name", name)
    try:
        expression.set_editor_property("group", group)
    except Exception:
        pass
    if expression_type == unreal.MaterialExpressionScalarParameter:
        expression.set_editor_property("default_value", float(default))
    elif expression_type == unreal.MaterialExpressionVectorParameter:
        expression.set_editor_property("default_value", default)
    return expression


def _texture_parameter(material, name, texture, x, y, group, normal=False):
    expression = _parameter_expression(
        material, unreal.MaterialExpressionTextureSampleParameter2D, name, None, x, y, group)
    expression.set_editor_property("texture", texture)
    if normal:
        expression.set_editor_property("sampler_type", unreal.MaterialSamplerType.SAMPLERTYPE_NORMAL)
    return expression


def _lerp_expression(material, a, a_output, b, b_output, alpha, x, y):
    expression = unreal.MaterialEditingLibrary.create_material_expression(
        material, unreal.MaterialExpressionLinearInterpolate, x, y)
    unreal.MaterialEditingLibrary.connect_material_expressions(a, a_output, expression, "A")
    unreal.MaterialEditingLibrary.connect_material_expressions(b, b_output, expression, "B")
    unreal.MaterialEditingLibrary.connect_material_expressions(alpha, "", expression, "Alpha")
    return expression


def _build_master_material(family, destination_root):
    names = {
        "pbr": "M_Forza_Master_PBR",
        "carPaint": "M_Forza_Master_CarPaint",
        "glass": "M_Forza_Master_Glass",
        "tire": "M_Forza_Master_Tire",
        "carbon": "M_Forza_Master_Carbon",
        "metal": "M_Forza_Master_Metal",
        "plastic": "M_Forza_Master_Plastic",
        "emissive": "M_Forza_Master_Emissive",
        "decal": "M_Forza_Master_Decal",
    }
    package_path = destination_root + "/MasterMaterials"
    asset_name = names[family]
    material = unreal.load_asset(package_path + "/" + asset_name)
    if not material:
        material = _asset_tools().create_asset(asset_name, package_path, unreal.Material, unreal.MaterialFactoryNew())
    if not material:
        raise RuntimeError("Could not create master material {}".format(asset_name))

    unreal.MaterialEditingLibrary.delete_all_material_expressions(material)
    if family == "glass":
        material.set_editor_property("blend_mode", unreal.BlendMode.BLEND_TRANSLUCENT)
        material.set_editor_property("two_sided", True)
    elif family == "decal":
        material.set_editor_property("blend_mode", unreal.BlendMode.BLEND_MASKED)
    else:
        material.set_editor_property("blend_mode", unreal.BlendMode.BLEND_OPAQUE)
    try:
        material.set_editor_property("shading_model", unreal.MaterialShadingModel.MSM_DEFAULT_LIT)
    except Exception:
        pass

    white = unreal.load_asset("/Engine/EngineResources/WhiteSquareTexture.WhiteSquareTexture")
    flat_normal = unreal.load_asset("/Engine/EngineMaterials/DefaultNormal.DefaultNormal") or white
    if not white:
        raise RuntimeError("Engine WhiteSquareTexture is unavailable")

    defaults = {
        "carPaint": ((0.18, 0.18, 0.18, 1.0), 0.18, 0.9),
        "glass": ((0.04, 0.06, 0.08, 1.0), 0.08, 0.0),
        "tire": ((0.02, 0.02, 0.02, 1.0), 0.72, 0.0),
        "carbon": ((0.035, 0.035, 0.035, 1.0), 0.28, 0.0),
        "metal": ((0.45, 0.45, 0.45, 1.0), 0.2, 1.0),
        "plastic": ((0.12, 0.12, 0.12, 1.0), 0.45, 0.0),
        "emissive": ((0.2, 0.2, 0.2, 1.0), 0.35, 0.0),
        "decal": ((1.0, 1.0, 1.0, 1.0), 0.45, 0.0),
        "pbr": ((0.5, 0.5, 0.5, 1.0), 0.5, 0.0),
    }
    color_default, rough_default, metal_default = defaults[family]

    color = _parameter_expression(material, unreal.MaterialExpressionVectorParameter, "BaseColor",
                                  unreal.LinearColor(*color_default), -900, -500, "Surface")
    base_tex = _texture_parameter(material, "BaseColorTex", white, -900, -350, "Textures")
    use_base = _parameter_expression(material, unreal.MaterialExpressionScalarParameter, "UseBaseColorTex",
                                     0.0, -900, -200, "Textures")
    base_lerp = _lerp_expression(material, color, "", base_tex, "RGB", use_base, -520, -400)
    unreal.MaterialEditingLibrary.connect_material_property(base_lerp, "", unreal.MaterialProperty.MP_BASE_COLOR)

    normal_tex = _texture_parameter(material, "NormalTex", flat_normal, -900, 0, "Textures", normal=True)
    unreal.MaterialEditingLibrary.connect_material_property(normal_tex, "RGB", unreal.MaterialProperty.MP_NORMAL)

    orm = _texture_parameter(material, "ORMTex", white, -900, 250, "Textures")
    use_orm = _parameter_expression(material, unreal.MaterialExpressionScalarParameter, "UseORMTex",
                                    0.0, -900, 400, "Textures")
    for output, parameter, default, prop, y in (
        ("R", "AmbientOcclusion", 1.0, unreal.MaterialProperty.MP_AMBIENT_OCCLUSION, 180),
        ("G", "Roughness", rough_default, unreal.MaterialProperty.MP_ROUGHNESS, 330),
        ("B", "Metallic", metal_default, unreal.MaterialProperty.MP_METALLIC, 480),
    ):
        scalar = _parameter_expression(material, unreal.MaterialExpressionScalarParameter, parameter,
                                       default, -520, y, "Surface")
        value = _lerp_expression(material, scalar, "", orm, output, use_orm, -180, y)
        unreal.MaterialEditingLibrary.connect_material_property(value, "", prop)

    if family == "glass":
        opacity = _parameter_expression(material, unreal.MaterialExpressionScalarParameter, "Opacity",
                                        0.28, -520, 680, "Transmission")
        unreal.MaterialEditingLibrary.connect_material_property(opacity, "", unreal.MaterialProperty.MP_OPACITY)
    elif family == "decal":
        unreal.MaterialEditingLibrary.connect_material_property(
            base_tex, "A", unreal.MaterialProperty.MP_OPACITY_MASK)
    elif family == "emissive":
        emissive_color = _parameter_expression(
            material, unreal.MaterialExpressionVectorParameter, "EmissiveColor",
            unreal.LinearColor(1.0, 1.0, 1.0, 1.0), -520, 680, "Emission")
        emissive_tex = _texture_parameter(material, "EmissiveTex", white, -900, 680, "Textures")
        use_emissive = _parameter_expression(
            material, unreal.MaterialExpressionScalarParameter, "UseEmissiveTex", 0.0, -900, 830, "Textures")
        emissive_lerp = _lerp_expression(
            material, emissive_color, "", emissive_tex, "RGB", use_emissive, -180, 700)
        strength = _parameter_expression(
            material, unreal.MaterialExpressionScalarParameter, "EmissiveStrength", 1.0, -520, 850, "Emission")
        multiply = unreal.MaterialEditingLibrary.create_material_expression(
            material, unreal.MaterialExpressionMultiply, 120, 740)
        unreal.MaterialEditingLibrary.connect_material_expressions(emissive_lerp, "", multiply, "A")
        unreal.MaterialEditingLibrary.connect_material_expressions(strength, "", multiply, "B")
        unreal.MaterialEditingLibrary.connect_material_property(
            multiply, "", unreal.MaterialProperty.MP_EMISSIVE_COLOR)

    unreal.MaterialEditingLibrary.recompile_material(material)
    unreal.EditorAssetLibrary.save_loaded_asset(material)
    return material


def _resolve_or_import_textures(inventory_path, document, existing_root, destination_root):
    package_name = document.get("texturePackage", {}).get("folder")
    if not package_name:
        package_name = os.path.splitext(os.path.basename(inventory_path))[0] + " Textures"
    disk_root = os.path.join(os.path.dirname(inventory_path), package_name)
    textures = {}
    reused = imported = missing = 0
    files = sorted(set(
        texture.get("file")
        for material in document.get("materials", [])
        for texture in material.get("resolvedTextures", [])
        if texture.get("resolved") and texture.get("file")))
    for relative in files:
        parts = relative.replace("\\", "/").split("/")
        folders = parts[1:-1]
        asset_name = _safe_name(os.path.splitext(parts[-1])[0])
        suffix = "/" + "/".join(_safe_name(part) for part in folders) if folders else ""
        texture = unreal.load_asset(existing_root + "/Textures" + suffix + "/" + asset_name)
        if not texture:
            texture = unreal.load_asset(destination_root + "/Textures" + suffix + "/" + asset_name)
        if texture:
            textures[relative] = texture
            reused += 1
            continue
        source = os.path.join(os.path.dirname(inventory_path), *parts)
        if os.path.isfile(source):
            imported_path = _import_texture(source, destination_root + "/Textures" + suffix)
            texture = unreal.load_asset(imported_path) if imported_path else None
            if texture:
                textures[relative] = texture
                imported += 1
                continue
        missing += 1
    _log("Texture reuse: {} existing, {} newly imported, {} missing.".format(reused, imported, missing))
    return textures


def _set_instance_parameters(instance, material_data, textures):
    family = _master_family(material_data)
    neutral = material_data.get("neutralFallback") or {}
    fallback = neutral.get("baseColorLinear")
    if family == "carPaint":
        value = _parameter_value(material_data, ("basecolor", "base_color", "diffuse", "tint", "colour", "color"))
        value = value if value is not None else (_primary_carpaint_color or fallback)
        color = _linear_color(value, (0.18, 0.18, 0.18, 1.0))
    elif family == "glass":
        color = _linear_color(_parameter_value(material_data, ("glasscolor", "glass_color", "tint", "colour", "color")),
                              (0.04, 0.06, 0.08, 1.0))
    else:
        color = _linear_color(_parameter_value(material_data, ("basecolor", "base_color", "diffuse", "tint", "colour", "color"), fallback),
                              (0.5, 0.5, 0.5, 1.0))
    unreal.MaterialEditingLibrary.set_material_instance_vector_parameter_value(instance, "BaseColor", color)

    defaults = {
        "carPaint": (0.18, 0.9), "glass": (0.08, 0.0), "tire": (0.72, 0.0),
        "carbon": (0.28, 0.0), "metal": (0.2, 1.0), "plastic": (0.45, 0.0),
        "emissive": (0.35, 0.0), "decal": (0.45, 0.0), "pbr": (0.5, 0.0),
    }
    rough_default, metal_default = defaults[family]
    unreal.MaterialEditingLibrary.set_material_instance_scalar_parameter_value(
        instance, "Roughness", _scalar(_parameter_value(material_data, ("roughness",)), neutral.get("roughness", rough_default)))
    unreal.MaterialEditingLibrary.set_material_instance_scalar_parameter_value(
        instance, "Metallic", _scalar(_parameter_value(material_data, ("metallic", "metalness")), neutral.get("metallic", metal_default)))
    unreal.MaterialEditingLibrary.set_material_instance_scalar_parameter_value(instance, "AmbientOcclusion", 1.0)
    if family == "glass":
        unreal.MaterialEditingLibrary.set_material_instance_scalar_parameter_value(
            instance, "Opacity", _scalar(_parameter_value(material_data, ("opacity", "alpha")), 0.28))

    bindings = _texture_bindings(material_data)
    for role, texture_data, binding in bindings:
        texture = textures.get(texture_data.get("file"))
        if not texture:
            continue
        _set_texture_settings(texture, binding)
        if role == "baseColor":
            unreal.MaterialEditingLibrary.set_material_instance_texture_parameter_value(instance, "BaseColorTex", texture)
            unreal.MaterialEditingLibrary.set_material_instance_scalar_parameter_value(instance, "UseBaseColorTex", 1.0)
        elif role == "normal":
            unreal.MaterialEditingLibrary.set_material_instance_texture_parameter_value(instance, "NormalTex", texture)
        elif role == "packedOrm":
            unreal.MaterialEditingLibrary.set_material_instance_texture_parameter_value(instance, "ORMTex", texture)
            unreal.MaterialEditingLibrary.set_material_instance_scalar_parameter_value(instance, "UseORMTex", 1.0)
        elif role == "emissive" and family == "emissive":
            unreal.MaterialEditingLibrary.set_material_instance_texture_parameter_value(instance, "EmissiveTex", texture)
            unreal.MaterialEditingLibrary.set_material_instance_scalar_parameter_value(instance, "UseEmissiveTex", 1.0)
    unreal.EditorAssetLibrary.save_loaded_asset(instance)


def import_forza_vehicle_instances(inventory_path, destination_root="/Game/ForzaVehicle",
                                   mesh_paths=None, existing_texture_root=None):
    """Build family masters + instances, reusing existing textures before importing missing PNGs."""
    inventory_path = os.path.abspath(inventory_path)
    with open(inventory_path, "r", encoding="utf-8") as source:
        document = json.load(source)
    if document.get("schema") not in ("ForzaTechStudio.UnrealVehicle", "ForzaTechStudio.MaterialInventory"):
        raise ValueError("Not a ForzaTech Studio Unreal vehicle manifest: {}".format(inventory_path))
    _configure_document_defaults(document)
    existing_texture_root = existing_texture_root or destination_root
    textures = _resolve_or_import_textures(inventory_path, document, existing_texture_root, destination_root)

    families = ("pbr", "carPaint", "glass", "tire", "carbon", "metal", "plastic", "emissive", "decal")
    masters = {family: _build_master_material(family, destination_root) for family in families}
    built = {}
    package_path = destination_root + "/MaterialInstances"
    for material_data in document.get("materials", []):
        material_id = material_data.get("id", "mat")
        asset_name = _safe_name("MI_{}_{}".format(material_id, material_data.get("name", "material")))
        instance = unreal.load_asset(package_path + "/" + asset_name)
        if not instance:
            instance = _asset_tools().create_asset(
                asset_name, package_path, unreal.MaterialInstanceConstant, unreal.MaterialInstanceConstantFactoryNew())
        if not instance:
            _warn("Could not create material instance {}".format(asset_name))
            continue
        instance.set_editor_property("parent", masters[_master_family(material_data)])
        _set_instance_parameters(instance, material_data, textures)
        built[material_id] = instance
    _assign_materials(document, built, mesh_paths)
    _log("Finished instance workflow: {} master(s), {} material instance(s).".format(len(masters), len(built)))
    return built


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

    _reset_expression_rows(material)

    family = _material_family(material_data)
    if family == "glass":
        material.set_editor_property("blend_mode", unreal.BlendMode.BLEND_TRANSLUCENT)
        try:
            material.set_editor_property("two_sided", True)
        except Exception:
            pass
    else:
        material.set_editor_property("blend_mode", unreal.BlendMode.BLEND_OPAQUE)

    bindings = _texture_bindings(material_data)
    for _, texture_data, binding in bindings:
        texture = textures.get(texture_data.get("file"))
        _set_texture_settings(texture, binding)

    base = _first_binding(bindings, "baseColor")
    fallback = (material_data.get("neutralFallback") or {}).get("baseColorLinear")
    if family == "carPaint":
        value = _parameter_value(
            material_data,
            ("basecolor", "base_color", "diffuse", "tint", "colour", "color"),
        )
        if value is None:
            value = _primary_carpaint_color or fallback
        _add_color_constant(material, _linear_color(value, (0.18, 0.18, 0.18, 1.0)))
    elif family == "glass":
        # Window and lamp glass often points at neutral engine placeholders.
        # Preserve an explicit GlassColor when supplied and otherwise use a
        # restrained blue-grey transmission tint.
        value = _parameter_value(material_data, ("glasscolor", "glass_color", "tint", "colour", "color"))
        _add_color_constant(material, _linear_color(value, (0.04, 0.06, 0.08, 1.0)))
    elif base and textures.get(base[0].get("file")):
        base_expression = _add_texture(material, textures[base[0]["file"]], base[1], unreal.MaterialProperty.MP_BASE_COLOR)
        if "opacityMask" in base[1].get("channels", ""):
            material.set_editor_property("blend_mode", unreal.BlendMode.BLEND_MASKED)
            unreal.MaterialEditingLibrary.connect_material_property(
                base_expression, "A", unreal.MaterialProperty.MP_OPACITY_MASK)
    else:
        color = _linear_color(_parameter_value(material_data, ("basecolor", "base_color", "diffuse", "tint", "colour", "color"), fallback),
                              (0.5, 0.5, 0.5, 1.0))
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
        neutral = material_data.get("neutralFallback") or {}
        roughness_default = neutral.get("roughness", 0.22 if family == "carPaint" else (0.12 if family == "glass" else 0.5))
        metallic_default = neutral.get("metallic", 0.9 if family == "carPaint" else 0.0)
        for role, property_name, tokens, default in (
            ("roughness", unreal.MaterialProperty.MP_ROUGHNESS, ("roughness",), roughness_default),
            ("metallic", unreal.MaterialProperty.MP_METALLIC, ("metallic", "metalness"), metallic_default),
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
        _add_constant(material, _scalar(_parameter_value(material_data, ("opacity", "alpha")), 0.28), unreal.MaterialProperty.MP_OPACITY)

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
    _configure_document_defaults(document)
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
