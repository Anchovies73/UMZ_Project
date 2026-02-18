import bpy
import os
from datetime import datetime
from bpy.props import StringProperty, BoolProperty, EnumProperty

from .constants import MODULE_ID, MODULE_NAME
from .storage import read_all_films_cached, mark_cache_dirty, get_external_folder

# Операции (пока импортируем из procedural_films_module через обратную ссылку нельзя — будет цикл)
# Поэтому на этом шаге импортируем из blender_ops, которого ещё нет.
# ВРЕМЕННО: будем импортировать функции из procedural_films_module через bpy.app.handlers нельзя.
# Поэтому проще: на этом шаге мы предполагаем, что операции будут в procedural_films/ops.py.
#
# Чтобы не тормозить, давай сразу сделаем минимальный ops.py в следующем подпункте.
from .ops import (
    create_animation_from_scene,
    update_animation_from_scene,
    apply_animation_to_scene,
    delete_animation,
)


# -------------------------
# UI props
# -------------------------

def films_items(self, context):
    films = read_all_films_cached()
    items = [(n, n, "") for n in films.keys()]
    if not items:
        items = [("", "(нет анимаций)", "")]
    return items


def register_scene_props():
    if not hasattr(bpy.types.Scene, "umz_selected_animation"):
        bpy.types.Scene.umz_selected_animation = EnumProperty(
            name="Анимация",
            items=films_items
        )
    if not hasattr(bpy.types.Scene, "umz_anim_full_delete"):
        bpy.types.Scene.umz_anim_full_delete = BoolProperty(
            name="Полное удаление",
            default=False
        )
    if not hasattr(bpy.types.Scene, "umz_export_alpha_tracks"):
        bpy.types.Scene.umz_export_alpha_tracks = BoolProperty(
            name="Экспорт прозрачности (alpha)",
            description="Экспорт alpha_tracks из Object Color (color[3]) или CP ['alpha'] в three_*.json",
            default=True
        )
    if not hasattr(bpy.types.Scene, "umz_anim_visible_selected_only"):
        bpy.types.Scene.umz_anim_visible_selected_only = BoolProperty(
            name="Только выделенные объекты",
            description="Если включено — в JSON сохраняются треки только для выделенных объектов и список видимых объектов (для three.js/Blender)",
            default=False
        )
    if not hasattr(bpy.types.Scene, "umz_text_and_markers"):
        bpy.types.Scene.umz_text_and_markers = BoolProperty(
            name="Текст и метки",
            description="Сохранять и загружать timeline markers и содержимое активного текстового редактора",
            default=False
        )

def unregister_scene_props():
    if hasattr(bpy.types.Scene, "umz_selected_animation"):
        try:
            del bpy.types.Scene.umz_selected_animation
        except Exception:
            pass
    if hasattr(bpy.types.Scene, "umz_anim_full_delete"):
        try:
            del bpy.types.Scene.umz_anim_full_delete
        except Exception:
            pass
    if hasattr(bpy.types.Scene, "umz_export_alpha_tracks"):
        try:
            del bpy.types.Scene.umz_export_alpha_tracks
        except Exception:
            pass
    if hasattr(bpy.types.Scene, "umz_anim_visible_selected_only"):
        try:
            del bpy.types.Scene.umz_anim_visible_selected_only
        except Exception:
            pass
    if hasattr(bpy.types.Scene, "umz_text_and_markers"):
        try:
            del bpy.types.Scene.umz_text_and_markers
        except Exception:
            pass
            


def format_created(timestamp):
    try:
        dt = datetime.fromisoformat(timestamp)
        return dt.strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        try:
            if "T" in timestamp:
                base = timestamp.split("T")[0]
                timepart = timestamp.split("T")[1].split(".")[0]
                return f"{base} {timepart}"
        except Exception:
            pass
    return timestamp


# -------------------------
# Operators
# -------------------------

class ANIM_OT_create(bpy.types.Operator):
    bl_idname = "umz.anim_create"
    bl_label = "Создать / Сохранить анимацию"
    name: StringProperty(name="Имя", default="animation1")
    description: StringProperty(name="Описание", default="")

    def execute(self, context):
        name = self.name
        internal = read_all_films_cached()  # чтобы решить create/update
        only_sel = bool(getattr(context.scene, "umz_anim_visible_selected_only", False))
        if name in internal:
            update_animation_from_scene(name, only_selected=only_sel)
            self.report({'INFO'}, f"Анимация '{name}' обновлена.")
        else:
            create_animation_from_scene(name, self.description, only_selected=only_sel)
            self.report({'INFO'}, f"Анимация '{name}' создана.")
        try:
            context.scene.umz_selected_animation = name
        except Exception:
            pass
        mark_cache_dirty()
        return {'FINISHED'}

    def invoke(self, context, event):
        return context.window_manager.invoke_props_dialog(self)


class ANIM_OT_load_delete(bpy.types.Operator):
    bl_idname = "umz.anim_load_delete"
    bl_label = "Загрузить/Удалить"
    anim: StringProperty()
    do_delete: BoolProperty(default=False)

    def execute(self, context):
        if self.do_delete:
            full = getattr(context.scene, "umz_anim_full_delete", False)
            ok = delete_animation(self.anim, full_delete=bool(full))
            if ok:
                all_names = list(read_all_films_cached().keys())
                try:
                    context.scene.umz_selected_animation = all_names[0] if all_names else ""
                except Exception:
                    pass
                if full:
                    self.report({'INFO'}, f"Анимация '{self.anim}' удалена и связанные Action удалены.")
                else:
                    self.report({'INFO'}, f"Анимация '{self.anim}' удалена (запись).")
                mark_cache_dirty()
                return {'FINISHED'}
            else:
                self.report({'ERROR'}, "Не найдено.")
                return {'CANCELLED'}
        else:
            try:
                res = apply_animation_to_scene(self.anim, remove_other_animations=True)
            except Exception as e:
                self.report({'ERROR'}, f"Ошибка при применении анимации: {e}")
                return {'CANCELLED'}
            self.report({'INFO'}, f"Загружено объектов: {len(res.get('applied', []))}")
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
        mark_cache_dirty()
        return {'FINISHED'}

    def invoke(self, context, event):
        context.window_manager.fileselect_add(self)
        return {'RUNNING_MODAL'}


# -------------------------
# Draw
# -------------------------

def draw_ui(layout, context):
    addon = __name__.split('.')[0]
    prefs = None
    try:
        prefs = bpy.context.preferences.addons[addon].preferences
    except Exception:
        pass

    col = layout.column(align=True)
    selected = getattr(context.scene, "umz_selected_animation", "")
    films = read_all_films_cached()
    btn_label = "Создать анимацию" if selected == "" or selected not in films else "Сохранить анимацию"
    col.operator("umz.anim_create", text=btn_label, icon='FILE_TICK')

    if not (prefs and prefs.external_animations_folder):
        col.operator("umz.anim_set_directory", icon='FILE_FOLDER')

    col.prop(context.scene, "umz_anim_visible_selected_only", text="Только выделенные объекты")
    col.prop(context.scene, "umz_text_and_markers", text="Текст и метки")
    col.prop(context.scene, "umz_export_alpha_tracks", text="Экспорт прозрачности (alpha)")
    col.prop(context.scene, "umz_anim_full_delete", text="Полное удаление")

    layout.separator()
    if not films:
        layout.label(text="Анимаций нет")
    else:
        if not hasattr(context.scene, "umz_selected_animation"):
            register_scene_props()
        row = layout.row(align=True)
        row.prop(context.scene, "umz_selected_animation", text="")
        ops = row.row(align=True)
        op_load = ops.operator("umz.anim_load_delete", text="", icon='FILE_TICK')
        op_load.anim = context.scene.umz_selected_animation
        op_load.do_delete = False
        op_del = ops.operator("umz.anim_load_delete", text="", icon='TRASH')
        op_del.anim = context.scene.umz_selected_animation
        op_del.do_delete = True
        film_data = films.get(context.scene.umz_selected_animation)
        if film_data:
            created = film_data.get("created_at", "(нет даты)")
            layout.label(text=f"Создано: {format_created(created)}")


# -------------------------
# Регистрация классов UI
# -------------------------

_classes = (ANIM_OT_create, ANIM_OT_load_delete, ANIM_OT_set_dir)
_registered = False
_register_cb = None


def register(register_callback):
    global _registered, _register_cb
    if _registered:
        return

    register_scene_props()

    for c in _classes:
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

    mark_cache_dirty()
    _registered = True


def unregister():
    global _registered, _register_cb
    if not _registered:
        return

    for c in reversed(_classes):
        try:
            bpy.utils.unregister_class(c)
        except Exception:
            pass

    unregister_scene_props()
    _registered = False
    _register_cb = None