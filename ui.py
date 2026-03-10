import bpy
import os
import time
from datetime import datetime
from bpy.props import (
    StringProperty,
    BoolProperty,
    IntProperty,
    CollectionProperty,
)

from .constants import MODULE_ID, MODULE_NAME, GLTF_ID_PROP
from . import storage as _storage
from .blender_codec import META_KEYS_ORDER
from .ops import (
    create_animation_from_scene,
    update_animation_from_scene,
    apply_animation_to_scene,
    delete_animation,
)

# =========================================================
# Changes:
# 1) ZIP dropdown auto-writes to obj["zip"] (no apply button).
# 2) Meta UI: shows active object name + 3 icon buttons (create/update/delete).
#    Update recalculates for ALL objects in the scene.
# =========================================================


class UMZ_AnimationItem(bpy.types.PropertyGroup):
    name: StringProperty(name="name", default="")


class ANIM_UL_umz_list(bpy.types.UIList):
    bl_idname = "ANIM_UL_umz_list"

    def draw_item(self, context, layout, data, item, icon, active_data, active_propname, index):
        layout.label(text=item.name, icon='ACTION')

    def filter_items(self, context, data, propname):
        items = getattr(data, propname)
        flt_flags = []
        flt_neworder = []

        filter_str = (self.filter_name or "").lower().strip()
        if not filter_str:
            flt_flags = [self.bitflag_filter_item] * len(items)
            return flt_flags, flt_neworder

        for it in items:
            name = (getattr(it, "name", "") or "").lower()
            flt_flags.append(self.bitflag_filter_item if filter_str in name else 0)

        return flt_flags, flt_neworder


def format_created(timestamp):
    try:
        dt = datetime.fromisoformat(timestamp)
        return dt.strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        return str(timestamp)


def _on_list_index_changed(self, context):
    sc = self
    try:
        idx = int(sc.umz_anim_list_index)
    except Exception:
        return
    if idx < 0 or idx >= len(sc.umz_anim_list):
        return
    try:
        sc.umz_selected_animation = sc.umz_anim_list[idx].name
    except Exception:
        pass


def register_scene_props():
    if not hasattr(bpy.types.Scene, "umz_anim_list"):
        bpy.types.Scene.umz_anim_list = CollectionProperty(type=UMZ_AnimationItem)

    if not hasattr(bpy.types.Scene, "umz_anim_list_index"):
        bpy.types.Scene.umz_anim_list_index = IntProperty(default=0, update=_on_list_index_changed)

    if not hasattr(bpy.types.Scene, "umz_selected_animation"):
        bpy.types.Scene.umz_selected_animation = StringProperty(name="Анимация", default="")

    if not hasattr(bpy.types.Scene, "umz_anim_full_delete"):
        bpy.types.Scene.umz_anim_full_delete = BoolProperty(name="Полное удаление", default=False)

    if not hasattr(bpy.types.Scene, "umz_export_alpha_tracks"):
        bpy.types.Scene.umz_export_alpha_tracks = BoolProperty(
            name="Экспорт прозрачности (alpha)",
            description="Экспорт alpha_tracks из Object Color (color[3]) или CP ['alpha'] в three_*.json",
            default=True
        )

    if not hasattr(bpy.types.Scene, "umz_anim_visible_selected_only"):
        bpy.types.Scene.umz_anim_visible_selected_only = BoolProperty(
            name="Только выделенные объекты",
            default=False
        )

    if not hasattr(bpy.types.Scene, "umz_text_and_markers"):
        bpy.types.Scene.umz_text_and_markers = BoolProperty(
            name="Текст и метки",
            default=False
        )

    if not hasattr(bpy.types.Scene, "umz_meta_ui_open"):
        bpy.types.Scene.umz_meta_ui_open = BoolProperty(name="Поля объекта", default=True)


def unregister_scene_props():
    for prop in (
        "umz_anim_list_index",
        "umz_anim_list",
        "umz_selected_animation",
        "umz_anim_full_delete",
        "umz_export_alpha_tracks",
        "umz_anim_visible_selected_only",
        "umz_text_and_markers",
        "umz_meta_ui_open",
        "umz_zip_choice",
    ):
        if hasattr(bpy.types.Scene, prop):
            try:
                delattr(bpy.types.Scene, prop)
            except Exception:
                pass


def _rebuild_list(scene, prefer_name=""):
    films = _storage.read_all_films_cached() or {}
    names = sorted(list(films.keys()))

    prev = prefer_name.strip() if prefer_name else (scene.umz_selected_animation or "")
    try:
        prev_idx = int(scene.umz_anim_list_index)
    except Exception:
        prev_idx = 0

    lst = scene.umz_anim_list
    lst.clear()
    for n in names:
        it = lst.add()
        it.name = n

    if not names:
        scene.umz_anim_list_index = 0
        scene.umz_selected_animation = ""
        return

    if prev and prev in names:
        idx = names.index(prev)
    else:
        idx = max(0, min(prev_idx, len(names) - 1))

    scene.umz_anim_list_index = idx
    scene.umz_selected_animation = names[idx]


# -------------------------
# gltf_id warning (cached)
# -------------------------

_GLTF_MISSING_CACHE = None
_GLTF_MISSING_CACHE_T = 0.0
_GLTF_MISSING_CACHE_TTL = 0.75


def _scene_has_missing_gltf_id(scene):
    global _GLTF_MISSING_CACHE, _GLTF_MISSING_CACHE_T
    now = time.time()
    if _GLTF_MISSING_CACHE is not None and (now - _GLTF_MISSING_CACHE_T) < _GLTF_MISSING_CACHE_TTL:
        return _GLTF_MISSING_CACHE

    missing = False
    try:
        for obj in scene.objects:
            try:
                if GLTF_ID_PROP not in obj.keys():
                    missing = True
                    break
            except Exception:
                missing = True
                break
    except Exception:
        missing = False

    _GLTF_MISSING_CACHE = missing
    _GLTF_MISSING_CACHE_T = now
    return missing


def _invalidate_gltf_cache():
    global _GLTF_MISSING_CACHE, _GLTF_MISSING_CACHE_T
    _GLTF_MISSING_CACHE = None
    _GLTF_MISSING_CACHE_T = 0.0


# -------------------------
# Meta helpers
# -------------------------

_META_LABELS_RU = {
    "position": "Позиция",
    "oboznachenie": "Обозначение",
    "naimenovanie": "Наименование",
    "count_in_animation": "Количество в анимации",
    "count_in_zip": "Количество в ЗИП",
    "zip": "ЗИП",
    "fnn": "ФНН",
    "proizvoditel": "Производитель",
    "link": "Ссылка",
}


def _split_obj_name(obj_name: str):
    s = (obj_name or "").strip()
    if not s:
        return "", ""
    if " " not in s:
        return s, ""
    left, right = s.split(" ", 1)
    return left.strip(), right.strip()


def _base_name_for_count(obj_name: str):
    # Name.001 -> Name
    return bpy.path.display_name_from_filepath(obj_name or "")


def _count_same_base_in_scene(scene, base: str):
    if not base:
        return 0
    cnt = 0
    for o in scene.objects:
        try:
            if _base_name_for_count(o.name) == base:
                cnt += 1
        except Exception:
            pass
    return cnt


def _compute_counts_for_scene(scene):
    """
    One pass: base_name -> count
    """
    res = {}
    for o in scene.objects:
        try:
            b = _base_name_for_count(o.name)
        except Exception:
            continue
        if not b:
            continue
        res[b] = res.get(b, 0) + 1
    return res


def _ensure_zip_ui_props_registered():
    if hasattr(bpy.types.Scene, "umz_zip_choice"):
        return

    def _zip_choice_update(self, context):
        # auto-apply to active object
        obj = context.object
        if not obj:
            return
        try:
            obj["zip"] = self.umz_zip_choice
        except Exception:
            pass

    bpy.types.Scene.umz_zip_choice = bpy.props.EnumProperty(
        name="ЗИП",
        items=[
            ("О", "О", ""),
            ("Г", "Г", ""),
            ("О/Г", "О/Г", ""),
        ],
        default="О",
        update=_zip_choice_update,
    )


def _sync_scene_zip_choice_from_obj(scene, obj):
    try:
        v = str(obj.get("zip", "О"))
    except Exception:
        v = "О"
    if v not in ("О", "Г", "О/Г"):
        v = "О"
    try:
        scene.umz_zip_choice = v
    except Exception:
        pass


# -------------------------
# Operators
# -------------------------

class ANIM_OT_refresh_list(bpy.types.Operator):
    bl_idname = "umz.anim_refresh_list"
    bl_label = "Синхронизировать"

    def execute(self, context):
        _storage.mark_cache_dirty()
        _rebuild_list(context.scene)
        return {'FINISHED'}


class ANIM_OT_set_dir(bpy.types.Operator):
    bl_idname = "umz.anim_set_directory"
    bl_label = "Папка для анимаций"
    filepath: StringProperty(subtype='FILE_PATH', default="")

    def execute(self, context):
        addon = __name__.split('.')[0]
        try:
            prefs = bpy.context.preferences.addons[addon].preferences
            if self.filepath:
                prefs.external_animations_folder = os.path.dirname(self.filepath)
                self.report({'INFO'}, f"Папка анимаций: {prefs.external_animations_folder}")
            else:
                self.report({'WARNING'}, "Путь не задан.")
        except Exception as e:
            self.report({'ERROR'}, f"{e}")
            return {'CANCELLED'}

        _storage.mark_cache_dirty()
        _rebuild_list(context.scene)
        return {'FINISHED'}

    def invoke(self, context, event):
        context.window_manager.fileselect_add(self)
        return {'RUNNING_MODAL'}


class ANIM_OT_clear_dir(bpy.types.Operator):
    bl_idname = "umz.anim_clear_directory"
    bl_label = "Очистить папку"

    def execute(self, context):
        ok = _storage.clear_external_folder_pref()
        if not ok:
            self.report({'ERROR'}, "Не удалось очистить настройку папки.")
            return {'CANCELLED'}
        _storage.mark_cache_dirty()
        _rebuild_list(context.scene)
        return {'FINISHED'}


class ANIM_OT_save_selected(bpy.types.Operator):
    bl_idname = "umz.anim_save_selected"
    bl_label = "Создать/Пересохранить"

    name: StringProperty(name="Имя", default="")
    description: StringProperty(name="Описание", default="")

    def execute(self, context):
        name = (self.name or "").strip()
        if not name:
            self.report({'ERROR'}, "Имя анимации не задано.")
            return {'CANCELLED'}

        only_sel = bool(getattr(context.scene, "umz_anim_visible_selected_only", False))

        internal = _storage.read_internal_films()
        if name in internal:
            update_animation_from_scene(name, only_selected=only_sel, description=self.description)
            self.report({'INFO'}, f"Анимация '{name}' обновлена.")
        else:
            create_animation_from_scene(name, self.description, only_selected=only_sel)
            self.report({'INFO'}, f"Анимация '{name}' создана.")

        _storage.mark_cache_dirty()
        _rebuild_list(context.scene, prefer_name=name)
        return {'FINISHED'}

    def invoke(self, context, event):
        sel = (getattr(context.scene, "umz_selected_animation", "") or "").strip()
        self.name = sel if sel else "animation1"

        try:
            film = (_storage.read_all_films_cached() or {}).get(self.name) or {}
            self.description = film.get("description", "") or ""
        except Exception:
            self.description = ""

        return context.window_manager.invoke_props_dialog(self)


class ANIM_OT_load_selected(bpy.types.Operator):
    bl_idname = "umz.anim_load_selected"
    bl_label = "Загрузить"

    def execute(self, context):
        sel = (getattr(context.scene, "umz_selected_animation", "") or "").strip()
        if not sel:
            self.report({'WARNING'}, "Анимация не выбрана.")
            return {'CANCELLED'}

        try:
            res = apply_animation_to_scene(sel, remove_other_animations=True)
        except Exception as e:
            self.report({'ERROR'}, f"Ошибка при применении анимации: {e}")
            return {'CANCELLED'}

        self.report({'INFO'}, f"Загружено объектов: {len(res.get('applied', []))}")
        _storage.mark_cache_dirty()
        _rebuild_list(context.scene, prefer_name=sel)
        return {'FINISHED'}


class ANIM_OT_delete_selected(bpy.types.Operator):
    bl_idname = "umz.anim_delete_selected"
    bl_label = "Удалить"

    def execute(self, context):
        scene = context.scene
        sel = (getattr(scene, "umz_selected_animation", "") or "").strip()
        if not sel:
            self.report({'WARNING'}, "Анимация не выбрана.")
            return {'CANCELLED'}

        try:
            idx_before = int(scene.umz_anim_list_index)
        except Exception:
            idx_before = 0

        full = getattr(scene, "umz_anim_full_delete", False)
        ok = delete_animation(sel, full_delete=bool(full))
        if not ok:
            self.report({'ERROR'}, "Не найдено.")
            return {'CANCELLED'}

        _storage.mark_cache_dirty()
        _rebuild_list(scene)

        names = [it.name for it in scene.umz_anim_list]
        if not names:
            scene.umz_anim_list_index = 0
            scene.umz_selected_animation = ""
            return {'FINISHED'}

        idx = idx_before
        if idx >= len(names):
            idx = len(names) - 1
        if idx < 0:
            idx = 0
        scene.umz_anim_list_index = idx
        scene.umz_selected_animation = names[idx]
        return {'FINISHED'}


class ANIM_OT_rename_selected(bpy.types.Operator):
    bl_idname = "umz.anim_rename_selected"
    bl_label = "Переименовать"

    new_name: StringProperty(name="Новое имя", default="")

    def execute(self, context):
        scene = context.scene
        old = (scene.umz_selected_animation or "").strip()
        new = (self.new_name or "").strip()

        if not old:
            self.report({'WARNING'}, "Анимация не выбрана.")
            return {'CANCELLED'}
        if not new:
            self.report({'ERROR'}, "Новое имя пустое.")
            return {'CANCELLED'}
        if new == old:
            return {'FINISHED'}

        internal = _storage.read_internal_films()
        if old not in internal:
            self.report({'ERROR'}, "Переименование поддерживается только для внутренних анимаций (internal).")
            return {'CANCELLED'}
        if new in internal:
            self.report({'ERROR'}, "Имя уже занято.")
            return {'CANCELLED'}

        entry = internal.pop(old)
        internal[new] = entry
        _storage.write_internal_films(internal)

        try:
            _storage.remove_animation_file(old)
            _storage.write_animation_to_file(new, entry)
        except Exception:
            pass

        _storage.mark_cache_dirty()
        _rebuild_list(scene, prefer_name=new)
        self.report({'INFO'}, f"Переименовано: {old} -> {new}")
        return {'FINISHED'}

    def invoke(self, context, event):
        scene = context.scene
        old = (scene.umz_selected_animation or "").strip()
        self.new_name = old
        return context.window_manager.invoke_props_dialog(self)


class ANIM_OT_fill_gltf_id_scene(bpy.types.Operator):
    bl_idname = "umz.anim_fill_gltf_id_scene"
    bl_label = "Установить gltf_id"

    def execute(self, context):
        count = 0
        for obj in context.scene.objects:
            try:
                obj[GLTF_ID_PROP] = obj.name
                count += 1
            except Exception:
                pass
        _invalidate_gltf_cache()
        self.report({'INFO'}, f"{GLTF_ID_PROP} установлен для объектов: {count}")
        return {'FINISHED'}


class ANIM_OT_init_meta_fields_on_active(bpy.types.Operator):
    bl_idname = "umz.anim_init_meta_fields"
    bl_label = "Создать поля"

    def execute(self, context):
        obj = context.object
        if not obj:
            self.report({'WARNING'}, "Нет активного объекта.")
            return {'CANCELLED'}

        counts = _compute_counts_for_scene(context.scene)

        obozn, naim = _split_obj_name(obj.name)
        base = _base_name_for_count(obj.name)
        qty = int(counts.get(base, 1))

        created = 0
        for k in META_KEYS_ORDER:
            try:
                if k not in obj.keys():
                    if k == "oboznachenie":
                        obj[k] = obozn
                    elif k == "naimenovanie":
                        obj[k] = naim
                    elif k == "count_in_animation":
                        obj[k] = qty
                    elif k == "count_in_zip":
                        obj[k] = qty
                    elif k == "zip":
                        obj[k] = "О"
                    else:
                        obj[k] = ""
                    created += 1
            except Exception:
                pass

        self.report({'INFO'}, f"Создано полей: {created}")
        return {'FINISHED'}


class ANIM_OT_update_meta_fields_all(bpy.types.Operator):
    bl_idname = "umz.anim_update_meta_fields_all"
    bl_label = "Обновить поля (все)"
    bl_description = "Обновить Обозначение/Наименование и количества для всех объектов сцены"

    def execute(self, context):
        scene = context.scene
        counts = _compute_counts_for_scene(scene)

        updated = 0
        for obj in scene.objects:
            try:
                keys = obj.keys()
            except Exception:
                continue

            # обновляем только если поля уже существуют у объекта
            if ("oboznachenie" not in keys) and ("naimenovanie" not in keys) and ("count_in_animation" not in keys) and ("count_in_zip" not in keys):
                continue

            obozn, naim = _split_obj_name(obj.name)
            base = _base_name_for_count(obj.name)
            qty = int(counts.get(base, 1))

            try:
                if "oboznachenie" in obj.keys():
                    obj["oboznachenie"] = obozn
                    updated += 1
            except Exception:
                pass
            try:
                if "naimenovanie" in obj.keys():
                    obj["naimenovanie"] = naim
                    updated += 1
            except Exception:
                pass
            try:
                if "count_in_animation" in obj.keys():
                    obj["count_in_animation"] = qty
                    updated += 1
            except Exception:
                pass
            try:
                if "count_in_zip" in obj.keys():
                    obj["count_in_zip"] = qty
                    updated += 1
            except Exception:
                pass

        self.report({'INFO'}, f"Обновлено значений: {updated}")
        return {'FINISHED'}


class ANIM_OT_remove_meta_fields_on_active(bpy.types.Operator):
    bl_idname = "umz.anim_remove_meta_fields"
    bl_label = "Удалить поля"

    def execute(self, context):
        obj = context.object
        if not obj:
            self.report({'WARNING'}, "Нет активного объекта.")
            return {'CANCELLED'}
        removed = 0
        for k in META_KEYS_ORDER:
            try:
                if k in obj.keys():
                    del obj[k]
                    removed += 1
            except Exception:
                pass
        self.report({'INFO'}, f"Удалено полей: {removed}")
        return {'FINISHED'}


def _draw_meta_fields(layout, context):
    _ensure_zip_ui_props_registered()

    scene = context.scene
    obj = context.object

    header = layout.row(align=True)
    is_open = bool(getattr(scene, "umz_meta_ui_open", True))
    icon = 'TRIA_DOWN' if is_open else 'TRIA_RIGHT'
    header.prop(scene, "umz_meta_ui_open", text="Поля объекта", icon=icon, emboss=False)

    if not is_open:
        return

    box = layout.box()
    if not obj:
        box.label(text="Нет активного объекта")
        return

    # имя объекта + 3 иконки без текста
    top = box.row(align=True)
    top.label(text=obj.name, icon='OBJECT_DATA')
    buttons = top.row(align=True)
    buttons.operator("umz.anim_init_meta_fields", text="", icon='ADD')
    buttons.operator("umz.anim_update_meta_fields_all", text="", icon='FILE_REFRESH')
    buttons.operator("umz.anim_remove_meta_fields", text="", icon='TRASH')

    missing_any = False
    for k in META_KEYS_ORDER:
        try:
            if k not in obj.keys():
                missing_any = True
                break
        except Exception:
            missing_any = True
            break

    if missing_any:
        box.label(text="Поля отсутствуют — нажмите +", icon='INFO')
        return

    _sync_scene_zip_choice_from_obj(scene, obj)

    col = box.column(align=True)
    for k in META_KEYS_ORDER:
        if k == "zip":
            col.separator(factor=0.15)
            col.label(text="ЗИП")
            col.prop(scene, "umz_zip_choice", text="")
            continue

        col.separator(factor=0.15)
        col.label(text=_META_LABELS_RU.get(k, k))
        try:
            col.prop(obj, f'["{k}"]', text="")
        except Exception:
            pass


def draw_ui(layout, context):
    scene = context.scene

    folder = _storage.get_external_folder()
    row_dir = layout.row(align=True)

    if folder:
        row_dir.label(text=folder, icon='FILE_FOLDER')
        row_dir.operator("umz.anim_set_directory", text="", icon='FILE_FOLDER')
        row_dir.operator("umz.anim_clear_directory", text="", icon='X')
    else:
        row_dir.scale_y = 1.4
        row_dir.operator("umz.anim_set_directory", text="Папка для анимаций", icon='FILE_FOLDER')

    box = layout.box()
    box.prop(scene, "umz_anim_visible_selected_only", text="Только выделенные объекты")
    box.prop(scene, "umz_export_alpha_tracks", text="Экспорт прозрачности (alpha)")
    box.prop(scene, "umz_text_and_markers", text="Текст и метки")
    box.prop(scene, "umz_anim_full_delete", text="Полное удаление")

    layout.separator()

    row = layout.row()
    row.template_list(
        "ANIM_UL_umz_list",
        "",
        scene,
        "umz_anim_list",
        scene,
        "umz_anim_list_index",
        rows=8
    )

    col_ops = row.column(align=True)
    col_ops.operator("umz.anim_save_selected", text="", icon='FILE_TICK')
    col_ops.operator("umz.anim_load_selected", text="", icon='IMPORT')
    col_ops.operator("umz.anim_delete_selected", text="", icon='TRASH')
    col_ops.separator()
    col_ops.operator("umz.anim_rename_selected", text="", icon='OUTLINER_DATA_FONT')
    col_ops.operator("umz.anim_refresh_list", text="", icon='FILE_REFRESH')

    sel = (scene.umz_selected_animation or "").strip()
    if sel:
        film = (_storage.read_all_films_cached() or {}).get(sel) or {}
        created = film.get("created_at")
        if created:
            layout.label(text=f"Создано: {format_created(created)}")
        desc = film.get("description", "")
        if desc:
            layout.label(text=f"Описание: {desc}")

    layout.separator()

    row_gltf = layout.row(align=True)
    row_gltf.operator("umz.anim_fill_gltf_id_scene", icon='SORTALPHA')
    if _scene_has_missing_gltf_id(scene):
        row_gltf.label(text="", icon='ERROR')

    _draw_meta_fields(layout, context)


_classes = (
    UMZ_AnimationItem,
    ANIM_UL_umz_list,
    ANIM_OT_refresh_list,
    ANIM_OT_set_dir,
    ANIM_OT_clear_dir,
    ANIM_OT_save_selected,
    ANIM_OT_load_selected,
    ANIM_OT_delete_selected,
    ANIM_OT_rename_selected,
    ANIM_OT_fill_gltf_id_scene,
    ANIM_OT_init_meta_fields_on_active,
    ANIM_OT_update_meta_fields_all,
    ANIM_OT_remove_meta_fields_on_active,
)

_registered = False
_register_cb = None


def register(register_callback):
    global _registered, _register_cb
    if _registered:
        return

    bpy.utils.register_class(UMZ_AnimationItem)
    bpy.utils.register_class(ANIM_UL_umz_list)
    register_scene_props()
    _ensure_zip_ui_props_registered()

    for c in _classes:
        if c in (UMZ_AnimationItem, ANIM_UL_umz_list):
            continue
        bpy.utils.register_class(c)

    _register_cb = register_callback
    try:
        register_callback({
            "id": MODULE_ID,
            "name": MODULE_NAME,
            "draw": draw_ui,
            "register": register,
            "unregister": unregister
        })
    except Exception:
        pass

    try:
        _storage.mark_cache_dirty()
        _rebuild_list(bpy.context.scene)
    except Exception:
        pass

    _registered = True


def unregister():
    global _registered, _register_cb
    if not _registered:
        return

    for c in reversed(_classes):
        if c in (UMZ_AnimationItem, ANIM_UL_umz_list):
            continue
        try:
            bpy.utils.unregister_class(c)
        except Exception:
            pass

    unregister_scene_props()

    try:
        bpy.utils.unregister_class(ANIM_UL_umz_list)
    except Exception:
        pass
    try:
        bpy.utils.unregister_class(UMZ_AnimationItem)
    except Exception:
        pass

    _registered = False
    _register_cb = None