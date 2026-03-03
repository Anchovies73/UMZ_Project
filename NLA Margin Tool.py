# -*- coding: utf-8 -*-
bl_info = {
    "name": "NLA Margin Tool",
    "author": "Anchovies73 + AI Assistant",
    "version": (1, 20),  # Финальная стабильная версия
    "blender": (2, 79, 0),
    "location": "Video Sequence Editor > N-panel > NLA Tools",
    "description": (
        "Добавляет отступы до/после пар маркеров. "
        "Сдвигает маркеры, NLA-стрипы и VSE-стрипы с учётом обрезок. "
        "Есть проверка пересечений курсора со стрипами (скрытые VSE не учитываются). "
        "У кастомных маркеров есть опция 'замок' — двигать только стрипы. "
        "Кнопка 'Укоротить' сдвигает правый маркер к концу последнего видимого стрипа внутри промежутка."
    ),
    "category": "Animation",
}

import bpy
import re


# ---------------------------------------------------------------------------
# Property Group for custom marker settings
# ---------------------------------------------------------------------------
class NLA_CustomMarkerSettings(bpy.types.PropertyGroup):
    index = bpy.props.IntProperty()           # индекс маркера (число после C_)
    lock_markers = bpy.props.BoolProperty(
        name="Замок",
        description="Если включено, двигать только стрипы, маркеры не сдвигаются",
        default=False
    )


# ---------------------------------------------------------------------------
# Main Property Group
# ---------------------------------------------------------------------------
class NLA_MARGIN_Properties(bpy.types.PropertyGroup):
    margin_frames = bpy.props.IntProperty(
        name="Отступ (кадры)",
        default=10,
        min=-100000,
        max=100000,
    )
    margin_frames_custom = bpy.props.IntProperty(
        name="Отступ (кадры)",
        default=10,
        min=-100000,
        max=100000,
    )
    marker_prefix = bpy.props.StringProperty(default="F_")
    shift_vse = bpy.props.BoolProperty(
        name="Двигать VSE стрипы",
        default=True,
    )
    # Коллекция для хранения настроек кастомных маркеров
    custom_marker_settings = bpy.props.CollectionProperty(
        type=NLA_CustomMarkerSettings,
        name="Custom Marker Settings"
    )


# ---------------------------------------------------------------------------
# Helper для получения настроек кастомного маркера
# ---------------------------------------------------------------------------
def get_custom_marker_settings(props, idx):
    """Возвращает элемент коллекции настроек для индекса idx (создаёт, если нет)"""
    for i in range(len(props.custom_marker_settings)):
        if props.custom_marker_settings[i].index == idx:
            return props.custom_marker_settings[i]
    new_item = props.custom_marker_settings.add()
    new_item.index = idx
    new_item.lock_markers = False
    return new_item


def remove_custom_marker_settings(props, idx):
    """Удаляет настройки для индекса idx из коллекции"""
    for i in range(len(props.custom_marker_settings) - 1, -1, -1):
        if props.custom_marker_settings[i].index == idx:
            props.custom_marker_settings.remove(i)
            break


# ---------------------------------------------------------------------------
# Marker helpers (F_)
# ---------------------------------------------------------------------------
def is_original_marker(marker_name, prefix):
    if not marker_name.startswith(prefix):
        return False
    return marker_name[len(prefix):].isdigit()


def is_anchor_marker(marker_name, prefix):
    return marker_name.startswith(prefix) and ("+" in marker_name)


def parse_original_index(marker_name, prefix):
    if not is_original_marker(marker_name, prefix):
        return None
    try:
        return int(marker_name[len(prefix):])
    except ValueError:
        return None


def get_original_markers_sorted(context):
    prefix = context.scene.nla_margin_props.marker_prefix
    tmp = []
    for m in context.scene.timeline_markers:
        idx = parse_original_index(m.name, prefix)
        if idx is not None:
            tmp.append((idx, m))
    tmp.sort(key=lambda x: x[0])
    return [m for _, m in tmp]


def get_anchor_markers(context):
    prefix = context.scene.nla_margin_props.marker_prefix
    return [m for m in context.scene.timeline_markers
            if is_anchor_marker(m.name, prefix)]


def get_anchor_map(context):
    prefix = context.scene.nla_margin_props.marker_prefix
    raw = {}
    for m in context.scene.timeline_markers:
        if not is_anchor_marker(m.name, prefix):
            continue
        base = m.name.split("+", 1)[0]
        raw.setdefault(base, []).append(m)
    result = {}
    for base, anchors in raw.items():
        if len(anchors) == 1:
            result[base] = anchors[0]
        else:
            anchors.sort(key=lambda a: abs(_get_anchor_offset(a)), reverse=True)
            result[base] = anchors[0]
            for dup in anchors[1:]:
                context.scene.timeline_markers.remove(dup)
    return result


def get_marker_gaps(context, originals=None):
    if originals is None:
        originals = get_original_markers_sorted(context)
    gaps = []
    for i in range(len(originals) - 1):
        gaps.append({
            "index":        i,
            "left_marker":  originals[i],
            "right_marker": originals[i + 1],
            "start_frame":  originals[i].frame,
            "end_frame":    originals[i + 1].frame,
            "duration":     originals[i + 1].frame - originals[i].frame,
        })
    return gaps


# ---------------------------------------------------------------------------
# Anchor helpers (F_)
# ---------------------------------------------------------------------------
def _get_anchor_offset(anchor_marker):
    m = re.search(r"\+(-?\d+)$", anchor_marker.name)
    return int(m.group(1)) if m else 0


def _set_anchor_name(anchor_marker, base_name, offset):
    anchor_marker.name = "{}+{}".format(base_name, int(offset))


def find_anchor(context, base_name):
    return get_anchor_map(context).get(base_name)


def ensure_anchor(context, base_name, frame):
    a = find_anchor(context, base_name)
    if a:
        return a
    return context.scene.timeline_markers.new(
        name="{}+0".format(base_name), frame=frame
    )


def update_anchor_offset(anchor_marker, base_name, delta):
    cur = _get_anchor_offset(anchor_marker)
    new_val = cur + delta
    _set_anchor_name(anchor_marker, base_name, new_val)
    return new_val


def maybe_delete_anchor_if_returned(context, original_marker, anchor_marker):
    if not anchor_marker:
        return False
    if original_marker.frame == anchor_marker.frame:
        context.scene.timeline_markers.remove(anchor_marker)
        return True
    return False


def clamp_negative_delta(original_marker, anchor_marker, requested_delta):
    if requested_delta > 0:
        return requested_delta
    if not anchor_marker:
        return 0
    dist = max(original_marker.frame - anchor_marker.frame, 0)
    return max(requested_delta, -dist)


# ---------------------------------------------------------------------------
# Custom anchor helpers (C_)
# ---------------------------------------------------------------------------
def is_custom_anchor(marker_name):
    if not marker_name.startswith("C_"):
        return False
    rest = marker_name[2:]
    return rest.isdigit()


def is_custom_offset_marker(marker_name):
    if not (marker_name.startswith("C+_") or marker_name.startswith("C-_")):
        return False
    return "+" in marker_name[3:]


def get_custom_anchors_sorted(context):
    tmp = []
    for m in context.scene.timeline_markers:
        if is_custom_anchor(m.name):
            try:
                idx = int(m.name[2:])
                tmp.append((idx, m))
            except ValueError:
                pass
    tmp.sort(key=lambda x: x[0])
    return [m for _, m in tmp]


def get_next_custom_anchor_index(context):
    existing = get_custom_anchors_sorted(context)
    if not existing:
        return 1
    indices = []
    for m in existing:
        try:
            indices.append(int(m.name[2:]))
        except ValueError:
            pass
    return max(indices) + 1 if indices else 1


def find_custom_offset_marker(context, anchor_index, direction):
    """direction: '+' или '-'"""
    prefix = "C{}_{}+".format(direction, anchor_index)
    for m in context.scene.timeline_markers:
        if m.name.startswith(prefix):
            return m
    return None


def get_custom_offset_value(offset_marker):
    m = re.search(r"\+(-?\d+)$", offset_marker.name)
    return int(m.group(1)) if m else 0


def set_custom_offset_name(offset_marker, anchor_index, direction, offset):
    offset_marker.name = "C{}_{}+{}".format(direction, anchor_index, int(offset))


# ---------------------------------------------------------------------------
# NLA strip shift - ИСПРАВЛЕНО И ОПТИМИЗИРОВАНО
# ---------------------------------------------------------------------------
def _move_strip(strip, delta):
    """
    Перемещает NLA стрип на delta кадров, сохраняя его размер и scale.
    В Blender 2.79 frame_end вычисляется автоматически — трогаем только frame_start.
    """
    if delta != 0:
        strip.frame_start += delta


def iter_scene_nla_strips(context):
    for obj in context.scene.objects:
        ad = getattr(obj, "animation_data", None)
        if not ad:
            continue
        tracks = getattr(ad, "nla_tracks", None)
        if not tracks:
            continue
        for track in tracks:
            for strip in track.strips:
                yield strip


def shift_nla_strips(context, threshold, delta):
    """
    Сдвигает NLA стрипы на delta кадров.
    Двигаются только стрипы, у которых frame_start >= threshold.
    Размер, scale и отступы между стрипами сохраняются.
    В Blender 2.79 frame_end является вычисляемым — трогаем только frame_start.
    """
    if delta == 0:
        return

    all_strips = list(iter_scene_nla_strips(context))
    if not all_strips:
        return

    to_move = [s for s in all_strips if s.frame_start >= threshold]
    if not to_move:
        return

    # Сортируем: при delta>0 справа налево, при delta<0 слева направо
    # Это предотвращает коллизии при перемещении
    reverse = delta > 0
    to_move.sort(key=lambda s: s.frame_start, reverse=reverse)

    for s in to_move:
        s.frame_start += delta


# ---------------------------------------------------------------------------
# VSE helpers – с учётом обрезок и видимости
# ---------------------------------------------------------------------------
def iter_vse_sequences(scene):
    se = scene.sequence_editor
    if not se:
        return []
    if hasattr(se, "sequences_all"):
        return list(se.sequences_all)
    return list(se.sequences)


def is_vse_visible(sequence):
    return not getattr(sequence, 'mute', False)


def get_vse_real_start(seq):
    """Возвращает реальное начало VSE стрипа с учётом обрезок"""
    return seq.frame_start + getattr(seq, 'frame_offset_start', 0)


def get_vse_real_end(seq):
    """Возвращает реальный конец VSE стрипа с учётом обрезок"""
    return getattr(seq, 'frame_final_end', seq.frame_start + seq.frame_duration)


def shift_vse_strips(context, threshold, delta):
    """
    Универсальная функция для сдвига VSE стрипов.
    
    Параметры:
        threshold: пороговый кадр
        delta: величина сдвига (может быть положительной или отрицательной)
    
    Алгоритм:
    1. Находим все видимые стрипы, реальное начало которых >= threshold
    2. Сортируем их по убыванию frame_start (от самых правых к левым)
    3. Двигаем каждый стрип на delta
    """
    if not context.scene.nla_margin_props.shift_vse or delta == 0:
        return 0
        
    if not context.scene.sequence_editor:
        return 0

    scene = context.scene
    seqs = list(iter_vse_sequences(scene))

    to_shift = []
    for s in seqs:
        if not is_vse_visible(s):
            continue
        real_start = get_vse_real_start(s)
        if real_start >= threshold:
            to_shift.append(s)

    # Сортировка: при delta>0 справа налево, при delta<0 слева направо
    reverse = delta > 0
    to_shift.sort(key=lambda s: s.frame_start, reverse=reverse)

    for s in to_shift:
        s.frame_start += delta

    # Принудительное обновление экрана
    current_frame = scene.frame_current
    scene.frame_current = current_frame + 1
    scene.frame_current = current_frame

    return len(to_shift)


def get_vse_strips_at_frame(context, frame):
    """Возвращает все VSE стрипы, начинающиеся на указанном кадре (с учётом обрезок)"""
    if not context.scene.sequence_editor:
        return []
    result = []
    for s in iter_vse_sequences(context.scene):
        real_start = get_vse_real_start(s)
        if real_start == frame:
            result.append(s)
    return result


# ---------------------------------------------------------------------------
# Intersection check functions
# ---------------------------------------------------------------------------
def check_nla_intersections(context, frame):
    intersecting = []
    for strip in iter_scene_nla_strips(context):
        if strip.frame_start < frame < strip.frame_end:
            intersecting.append(strip)
    return intersecting


def check_vse_intersections(context, frame):
    if not context.scene.sequence_editor:
        return []
    
    intersecting = []
    for seq in iter_vse_sequences(context.scene):
        if not is_vse_visible(seq):
            continue
        real_start = get_vse_real_start(seq)
        real_end = get_vse_real_end(seq)
        if real_start < frame < real_end:
            intersecting.append(seq)
    return intersecting


def check_cursor_intersections(context, gap_left_frame, gap_right_frame):
    cursor_frame = context.scene.frame_current
    if not (gap_left_frame < cursor_frame < gap_right_frame):
        return False, [], []
    
    nla_intersections = check_nla_intersections(context, cursor_frame)
    vse_intersections = check_vse_intersections(context, cursor_frame)
    
    return (len(nla_intersections) > 0 or len(vse_intersections) > 0, 
            nla_intersections, 
            vse_intersections)


# ---------------------------------------------------------------------------
# Group custom anchors by gaps
# ---------------------------------------------------------------------------
def get_custom_anchors_by_gap(context, gaps):
    custom_anchors = get_custom_anchors_sorted(context)
    if not gaps or not custom_anchors:
        return {}
    
    result = {i: [] for i in range(len(gaps))}
    for m in custom_anchors:
        for i, gap in enumerate(gaps):
            if gap["start_frame"] < m.frame < gap["end_frame"]:
                result[i].append(m)
                break
    return result


# ---------------------------------------------------------------------------
# Helper for finding the last strip end inside the gap (for shrinking)
# ---------------------------------------------------------------------------
def get_last_strip_end_inside_gap(context, left_frame, right_frame):
    """
    Возвращает наибольший кадр конца видимого стрипа (NLA или VSE),
    который находится строго внутри промежутка (left_frame < end < right_frame).
    Скрытые VSE стрипы (mute=True) игнорируются.
    Если таких стрипов нет, возвращает None.
    """
    last_end = None

    # NLA стрипы
    for strip in iter_scene_nla_strips(context):
        if left_frame < strip.frame_end < right_frame:
            if last_end is None or strip.frame_end > last_end:
                last_end = strip.frame_end

    # VSE стрипы (только видимые)
    if context.scene.sequence_editor:
        for seq in iter_vse_sequences(context.scene):
            if not is_vse_visible(seq):
                continue
            real_end = get_vse_real_end(seq)
            if left_frame < real_end < right_frame:
                if last_end is None or real_end > last_end:
                    last_end = real_end

    return last_end


# ---------------------------------------------------------------------------
# Определение текущего промежутка по положению курсора
# ---------------------------------------------------------------------------
def get_current_gap_index(context, gaps):
    """
    Возвращает индекс промежутка, в котором находится курсор,
    или -1, если курсор не внутри ни одного промежутка.
    """
    cursor_frame = context.scene.frame_current
    for i, gap in enumerate(gaps):
        if gap["start_frame"] < cursor_frame < gap["end_frame"]:
            return i
    return -1


# ---------------------------------------------------------------------------
# Marker shift helpers
# ---------------------------------------------------------------------------
def shift_markers(context, threshold, delta):
    """
    Универсальная функция для сдвига маркеров.
    Сдвигает все F_ маркеры, которые находятся на threshold или правее.
    """
    if delta == 0:
        return
        
    prefix = context.scene.nla_margin_props.marker_prefix
    markers = [m for m in context.scene.timeline_markers
               if is_original_marker(m.name, prefix) and m.frame >= threshold]
    
    # Сортировка по убыванию (справа налево)
    markers.sort(key=lambda m: m.frame, reverse=True)
    
    for m in markers:
        m.frame += delta


# ---------------------------------------------------------------------------
# High-level: универсальная функция сдвига
# ---------------------------------------------------------------------------
def shift_all_right_of(context, threshold, delta, move_markers=True):
    """
    Универсальная функция для сдвига всего, что находится на threshold или правее.
    
    Параметры:
        threshold: пороговый кадр
        delta: величина сдвига (может быть положительной или отрицательной)
        move_markers: сдвигать ли маркеры
    
    Возвращает количество сдвинутых VSE стрипов.
    """
    # Сдвигаем NLA стрипы
    shift_nla_strips(context, threshold, delta)
    
    # Сдвигаем маркеры (если нужно)
    if move_markers:
        shift_markers(context, threshold, delta)
    
    # Сдвигаем VSE стрипы
    vse_count = shift_vse_strips(context, threshold, delta)
    
    return vse_count


# ---------------------------------------------------------------------------
# Operators — F_ промежутки
# ---------------------------------------------------------------------------
class NLA_OT_add_margin_after_gap(bpy.types.Operator):
    """Удлинить промежуток: сдвинуть правый маркер и всё за ним вправо"""
    bl_idname = "nla.add_margin_after_gap"
    bl_label = "Удлинить →"
    bl_options = {'REGISTER', 'UNDO'}

    gap_index = bpy.props.IntProperty()
    delta = bpy.props.IntProperty(default=10)

    def execute(self, context):
        requested = int(self.delta)

        originals = get_original_markers_sorted(context)
        gaps = get_marker_gaps(context, originals=originals)

        if self.gap_index >= len(gaps):
            self.report({'ERROR'}, "Неверный индекс: {}".format(self.gap_index))
            return {'CANCELLED'}

        right_marker = gaps[self.gap_index]["right_marker"]

        # Якорная логика (для отрицательных сдвигов)
        anchor = find_anchor(context, right_marker.name)
        if requested > 0 and anchor is None:
            anchor = ensure_anchor(context, right_marker.name, right_marker.frame)

        applied = clamp_negative_delta(right_marker, anchor, requested)
        if applied == 0:
            self.report({'INFO'}, "Удлинение: нечего менять")
            return {'CANCELLED'}

        if anchor is not None:
            update_anchor_offset(anchor, right_marker.name, applied)

        threshold = right_marker.frame
        vse_count = shift_all_right_of(context, threshold, applied, move_markers=True)

        deleted = maybe_delete_anchor_if_returned(context, right_marker, anchor)

        vse_at_frame = get_vse_strips_at_frame(context, threshold)
        vse_msg = ""
        if vse_at_frame:
            vse_msg = " (сдвинуто {} VSE стрипов на границе)".format(len(vse_at_frame))
        elif vse_count > 0:
            vse_msg = " (сдвинуто {} VSE стрипов)".format(vse_count)

        self.report({'INFO'}, "Удлинение gap={} applied={}{}{}".format(
            self.gap_index, applied, " (якорь удалён)" if deleted else "", vse_msg))
        return {'FINISHED'}


class NLA_OT_shrink_gap(bpy.types.Operator):
    """Укоротить промежуток: сдвинуть правый маркер к концу последнего видимого стрипа внутри промежутка"""
    bl_idname = "nla.shrink_gap"
    bl_label = "← Укоротить"
    bl_options = {'REGISTER', 'UNDO'}

    gap_index = bpy.props.IntProperty()

    def execute(self, context):
        props = context.scene.nla_margin_props
        originals = get_original_markers_sorted(context)
        gaps = get_marker_gaps(context, originals=originals)

        if self.gap_index >= len(gaps):
            self.report({'ERROR'}, "Неверный индекс: {}".format(self.gap_index))
            return {'CANCELLED'}

        left_marker = gaps[self.gap_index]["left_marker"]
        right_marker = gaps[self.gap_index]["right_marker"]

        # Находим конец последнего видимого стрипа внутри промежутка
        last_strip_end = get_last_strip_end_inside_gap(context, left_marker.frame, right_marker.frame)
        
        if last_strip_end is None:
            self.report({'INFO'}, "Нет стрипов внутри промежутка для укорачивания")
            return {'CANCELLED'}
        
        # Диагностика – какой стрип найден
        debug_info = ""
        for strip in iter_scene_nla_strips(context):
            if strip.frame_end == last_strip_end:
                debug_info = " (NLA: " + strip.name + ")"
                break
        if not debug_info and context.scene.sequence_editor:
            for seq in iter_vse_sequences(context.scene):
                if not is_vse_visible(seq):
                    continue
                real_end = get_vse_real_end(seq)
                if real_end == last_strip_end:
                    debug_info = " (VSE: " + seq.name + ")"
                    break

        # Новый правый маркер = конец стрипа + отступ
        new_right = last_strip_end + props.margin_frames
        
        # Проверка, чтобы новый правый маркер не оказался левее левого
        if new_right <= left_marker.frame:
            new_right = left_marker.frame + 1
            self.report({'INFO'}, "Отступ слишком велик, установлена минимальная длина")
            return {'CANCELLED'}
            
        if new_right >= right_marker.frame:
            self.report({'INFO'}, "Отступ слишком велик, промежуток не может быть уменьшен")
            return {'CANCELLED'}

        # Вычисляем сдвиг (отрицательный)
        delta = new_right - right_marker.frame
        
        # Сдвигаем всё, что справа от правого маркера
        vse_count = shift_all_right_of(context, right_marker.frame, delta, move_markers=True)
        
        vse_msg = ""
        if vse_count > 0:
            vse_msg = " (сдвинуто {} VSE стрипов)".format(vse_count)

        self.report({'INFO'}, "Укорочение gap={} до кадра {} (последний стрип внутри {} + отступ {}){}{}".format(
            self.gap_index, new_right, last_strip_end, props.margin_frames, debug_info, vse_msg))
        return {'FINISHED'}


class NLA_OT_remove_anchor_markers(bpy.types.Operator):
    bl_idname = "nla.remove_anchor_markers"
    bl_label = "Удалить все F_ якоря"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        anchors = get_anchor_markers(context)
        count = len(anchors)
        for a in anchors:
            context.scene.timeline_markers.remove(a)
        self.report({'INFO'}, "Удалено F_ якорей: {}".format(count))
        return {'FINISHED'}


class NLA_OT_quick_adjust_margin(bpy.types.Operator):
    bl_idname = "nla.quick_adjust_margin"
    bl_label = "Быстрая правка отступа"
    bl_options = {'REGISTER', 'INTERNAL'}

    delta = bpy.props.IntProperty(default=1)

    def execute(self, context):
        context.scene.nla_margin_props.margin_frames += self.delta
        return {'FINISHED'}


# ---------------------------------------------------------------------------
# Operators — C_ кастомные якоря
# ---------------------------------------------------------------------------
class NLA_OT_create_custom_anchor(bpy.types.Operator):
    bl_idname = "nla.create_custom_anchor"
    bl_label = "Создать якорь C_N на курсоре"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        frame = context.scene.frame_current
        idx = get_next_custom_anchor_index(context)
        name = "C_{}".format(idx)
        context.scene.timeline_markers.new(name=name, frame=frame)
        props = context.scene.nla_margin_props
        get_custom_marker_settings(props, idx)
        self.report({'INFO'}, "Создан {} на кадре {}".format(name, frame))
        return {'FINISHED'}


class NLA_OT_quick_adjust_margin_custom(bpy.types.Operator):
    bl_idname = "nla.quick_adjust_margin_custom"
    bl_label = "Быстрая правка отступа (C_)"
    bl_options = {'REGISTER', 'INTERNAL'}

    delta = bpy.props.IntProperty(default=1)

    def execute(self, context):
        context.scene.nla_margin_props.margin_frames_custom += self.delta
        return {'FINISHED'}


class NLA_OT_custom_margin_after(bpy.types.Operator):
    """Сдвинуть всё строго правее C_N вправо. C_N неподвижен."""
    bl_idname = "nla.custom_margin_after"
    bl_label = "ПОСЛЕ →"
    bl_options = {'REGISTER', 'UNDO'}

    anchor_index = bpy.props.IntProperty()
    delta = bpy.props.IntProperty(default=10)

    def execute(self, context):
        requested = int(self.delta)
        if requested == 0:
            return {'CANCELLED'}

        anchor_marker = None
        for m in context.scene.timeline_markers:
            if m.name == "C_{}".format(self.anchor_index):
                anchor_marker = m
                break

        if anchor_marker is None:
            self.report({'ERROR'}, "Якорь C_{} не найден".format(self.anchor_index))
            return {'CANCELLED'}

        props = context.scene.nla_margin_props
        settings = get_custom_marker_settings(props, self.anchor_index)
        move_markers = not settings.lock_markers

        offset_marker = find_custom_offset_marker(context, self.anchor_index, "+")
        cur_offset = 0
        if offset_marker:
            cur_offset = get_custom_offset_value(offset_marker)

        new_offset = cur_offset + requested

        vse_at_frame = get_vse_strips_at_frame(context, anchor_marker.frame)
        vse_count = len(vse_at_frame)

        total_vse = shift_all_right_of(context, anchor_marker.frame, requested, move_markers)

        if offset_marker is None:
            new_frame = anchor_marker.frame + new_offset
            offset_marker = context.scene.timeline_markers.new(
                name="C+_{}+{}".format(self.anchor_index, new_offset),
                frame=new_frame
            )
        else:
            new_frame = anchor_marker.frame + new_offset
            set_custom_offset_name(offset_marker, self.anchor_index, "+", new_offset)
            offset_marker.frame = new_frame

        if new_offset == 0 and offset_marker:
            context.scene.timeline_markers.remove(offset_marker)

        vse_msg = ""
        if vse_count > 0:
            vse_msg = " (сдвинуто {} VSE стрипов на границе)".format(vse_count)
        elif total_vse > 0:
            vse_msg = " (сдвинуто {} VSE стрипов)".format(total_vse)

        lock_status = " (маркеры не тронуты)" if not move_markers else ""
        self.report({'INFO'}, "C_{} ПОСЛЕ applied={}, отступ на кадре {}{}{}".format(
            self.anchor_index, requested, new_frame, vse_msg, lock_status))
        return {'FINISHED'}


class NLA_OT_custom_margin_before(bpy.types.Operator):
    """Сдвинуть всё строго левее C_N влево. C_N неподвижен."""
    bl_idname = "nla.custom_margin_before"
    bl_label = "← ПЕРЕД"
    bl_options = {'REGISTER', 'UNDO'}

    anchor_index = bpy.props.IntProperty()
    delta = bpy.props.IntProperty(default=10)

    def execute(self, context):
        requested = int(self.delta)
        if requested == 0:
            return {'CANCELLED'}

        anchor_marker = None
        for m in context.scene.timeline_markers:
            if m.name == "C_{}".format(self.anchor_index):
                anchor_marker = m
                break

        if anchor_marker is None:
            self.report({'ERROR'}, "Якорь C_{} не найден".format(self.anchor_index))
            return {'CANCELLED'}

        props = context.scene.nla_margin_props
        settings = get_custom_marker_settings(props, self.anchor_index)
        move_markers = not settings.lock_markers

        offset_marker = find_custom_offset_marker(context, self.anchor_index, "-")
        cur_offset = 0
        if offset_marker:
            cur_offset = get_custom_offset_value(offset_marker)

        new_offset = cur_offset + requested

        total_vse = shift_all_right_of(context, anchor_marker.frame, -requested, move_markers)

        if offset_marker is None:
            new_frame = anchor_marker.frame - new_offset
            offset_marker = context.scene.timeline_markers.new(
                name="C-_{}+{}".format(self.anchor_index, new_offset),
                frame=new_frame
            )
        else:
            new_frame = anchor_marker.frame - new_offset
            set_custom_offset_name(offset_marker, self.anchor_index, "-", new_offset)
            offset_marker.frame = new_frame

        if new_offset == 0 and offset_marker:
            context.scene.timeline_markers.remove(offset_marker)

        lock_status = " (маркеры не тронуты)" if not move_markers else ""
        self.report({'INFO'}, "C_{} ПЕРЕД applied={}, отступ на кадре {} (сдвинуто {} VSE){}".format(
            self.anchor_index, requested, new_frame, total_vse, lock_status))
        return {'FINISHED'}


class NLA_OT_delete_custom_anchor(bpy.types.Operator):
    bl_idname = "nla.delete_custom_anchor"
    bl_label = "Удалить"
    bl_options = {'REGISTER', 'UNDO'}

    anchor_index = bpy.props.IntProperty()

    def execute(self, context):
        to_delete = []
        anchor_name = "C_{}".format(self.anchor_index)
        for m in context.scene.timeline_markers:
            if m.name == anchor_name:
                to_delete.append(m)
            elif m.name.startswith("C+_{}+".format(self.anchor_index)):
                to_delete.append(m)
            elif m.name.startswith("C-_{}+".format(self.anchor_index)):
                to_delete.append(m)
        count = len(to_delete)
        for m in to_delete:
            context.scene.timeline_markers.remove(m)

        remove_custom_marker_settings(context.scene.nla_margin_props, self.anchor_index)

        self.report({'INFO'}, "Удалено для C_{}: {}".format(self.anchor_index, count))
        return {'FINISHED'}


class NLA_OT_delete_all_custom(bpy.types.Operator):
    bl_idname = "nla.delete_all_custom"
    bl_label = "Удалить все C_ якоря и отступы"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        to_delete = [m for m in context.scene.timeline_markers
                     if is_custom_anchor(m.name) or is_custom_offset_marker(m.name)]
        count = len(to_delete)
        for m in to_delete:
            context.scene.timeline_markers.remove(m)

        context.scene.nla_margin_props.custom_marker_settings.clear()

        self.report({'INFO'}, "Удалено C_ маркеров: {}".format(count))
        return {'FINISHED'}


# ---------------------------------------------------------------------------
# Dummy operator for the middle button (no functionality)
# ---------------------------------------------------------------------------
class NLA_OT_dummy(bpy.types.Operator):
    """Промежуточная кнопка без функционала (для отображения маркера)"""
    bl_idname = "nla.dummy"
    bl_label = ""
    bl_options = {'REGISTER', 'INTERNAL'}
    
    gap_index = bpy.props.IntProperty()
    
    def execute(self, context):
        # Ничего не делаем, просто возвращаем FINISHED
        return {'FINISHED'}


# ---------------------------------------------------------------------------
# Panel
# ---------------------------------------------------------------------------
class NLA_PT_main_panel(bpy.types.Panel):
    bl_label = "NLA Margin Tool"
    bl_idname = "NLA_PT_main_panel"
    bl_space_type = 'SEQUENCE_EDITOR'
    bl_region_type = 'UI'
    bl_category = "NLA Tools"

    @classmethod
    def poll(cls, context):
        return context.scene is not None

    def draw(self, context):
        layout = self.layout
        props = context.scene.nla_margin_props

        # Настройки
        box = layout.box()
        box.label(text="Настройки")
        box.prop(props, "marker_prefix")
        box.prop(props, "shift_vse")

        layout.separator()
        layout.label(text="── Промежутки F_ ──")

        originals = get_original_markers_sorted(context)
        gaps = get_marker_gaps(context, originals=originals)

        if not gaps:
            layout.label(text="Нет промежутков (нужно ≥ 2 маркера F_N)")
        else:
            custom_by_gap = get_custom_anchors_by_gap(context, gaps)
            current_gap = get_current_gap_index(context, gaps)
            
            for i, gap in enumerate(gaps):
                left = gap["left_marker"]
                right = gap["right_marker"]
                
                has_intersection, nla_strips, vse_strips = check_cursor_intersections(
                    context, left.frame, right.frame
                )

                gbox = layout.box()
                
                # Заголовок с информацией о промежутке
                header_row = gbox.row()
                
                # Если это текущий промежуток - делаем заголовок красным с иконкой
                if i == current_gap:
                    header_row.alert = True
                    header_row.label(text="{} (кадр {})  →  {} (кадр {})   [{} кадров]".format(
                        left.name, left.frame,
                        right.name, right.frame,
                        gap["duration"]), icon='TRIA_RIGHT')
                else:
                    header_row.label(text="{} (кадр {})  →  {} (кадр {})   [{} кадров]".format(
                        left.name, left.frame,
                        right.name, right.frame,
                        gap["duration"]))
                
                if has_intersection:
                    warning_row = gbox.row()
                    warning_row.alert = True
                    warning_row.label(text="Есть пересечение", icon='ERROR')

                # Кнопка создания кастомного маркера
                create_row = gbox.row(align=True)
                create_row.operator("nla.create_custom_anchor", text="Добавить C_ маркер", icon='MARKER_HLT')
                
                # Основные элементы управления отступом
                row = gbox.row(align=True)
                row.prop(props, "margin_frames", text="Отступ")
                for d, lbl in ((-10, "-10"), (-1, "-1"), (1, "+1"), (10, "+10")):
                    op = row.operator("nla.quick_adjust_margin", text=lbl)
                    op.delta = d

                # Три кнопки в ряд: Укоротить | Маркер | Удлинить
                button_row = gbox.row(align=True)
                
                # Левая кнопка - Укоротить (большая)
                op_shrink = button_row.operator("nla.shrink_gap", text="← Укоротить")
                op_shrink.gap_index = i
                
                # Средняя кнопка - без функционала, с названием правого маркера
                marker_op = button_row.operator("nla.dummy", text=right.name)
                marker_op.gap_index = i
                
                # Правая кнопка - Удлинить (большая)
                op_enlarge = button_row.operator("nla.add_margin_after_gap", text="Удлинить →")
                op_enlarge.gap_index = i
                op_enlarge.delta = props.margin_frames

                # Кастомные маркеры внутри этого промежутка
                if i in custom_by_gap and custom_by_gap[i]:
                    gbox.separator()
                    for m in custom_by_gap[i]:
                        try:
                            idx = int(m.name[2:])
                        except ValueError:
                            continue

                        off_after = find_custom_offset_marker(context, idx, "+")
                        off_before = find_custom_offset_marker(context, idx, "-")

                        cbox = gbox.box()

                        # Строка с названием и галочкой
                        header_row = cbox.row(align=True)
                        header_row.label(text="{}  (кадр {})".format(m.name, m.frame))
                        if off_before:
                            header_row.label(text="←{}".format(get_custom_offset_value(off_before)))
                        if off_after:
                            header_row.label(text="{}→".format(get_custom_offset_value(off_after)))
                        
                        # Галочка "замок"
                        settings = get_custom_marker_settings(props, idx)
                        header_row.prop(settings, "lock_markers", text="", icon='LOCKED' if settings.lock_markers else 'UNLOCKED')

                        row = cbox.row(align=True)
                        row.prop(props, "margin_frames_custom", text="Отступ")
                        for d, lbl in ((-10, "-10"), (-1, "-1"), (1, "+1"), (10, "+10")):
                            op = row.operator("nla.quick_adjust_margin_custom", text=lbl)
                            op.delta = d

                        row2 = cbox.row(align=True)
                        op_b = row2.operator("nla.custom_margin_before", text="← ПЕРЕД")
                        op_b.anchor_index = idx
                        op_b.delta = props.margin_frames_custom

                        op_a = row2.operator("nla.custom_margin_after", text="ПОСЛЕ →")
                        op_a.anchor_index = idx
                        op_a.delta = props.margin_frames_custom

                        op_del = cbox.operator(
                            "nla.delete_custom_anchor",
                            text="Удалить {} + отступы".format(m.name),
                            icon='X',
                        )
                        op_del.anchor_index = idx

        layout.separator()
        layout.operator("nla.remove_anchor_markers", icon='X')
        layout.separator()
        layout.operator("nla.delete_all_custom", icon='X')


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------
classes = (
    NLA_CustomMarkerSettings,
    NLA_MARGIN_Properties,
    NLA_OT_quick_adjust_margin,
    NLA_OT_quick_adjust_margin_custom,
    NLA_OT_add_margin_after_gap,
    NLA_OT_shrink_gap,
    NLA_OT_remove_anchor_markers,
    NLA_OT_create_custom_anchor,
    NLA_OT_custom_margin_after,
    NLA_OT_custom_margin_before,
    NLA_OT_delete_custom_anchor,
    NLA_OT_delete_all_custom,
    NLA_OT_dummy,
    NLA_PT_main_panel,
)


def register():
    for c in classes:
        bpy.utils.register_class(c)
    bpy.types.Scene.nla_margin_props = bpy.props.PointerProperty(
        type=NLA_MARGIN_Properties
    )


def unregister():
    del bpy.types.Scene.nla_margin_props
    for c in reversed(classes):
        bpy.utils.unregister_class(c)


if __name__ == "__main__":
    register()