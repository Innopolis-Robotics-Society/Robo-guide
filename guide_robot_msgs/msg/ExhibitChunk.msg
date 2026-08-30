# Один фрагмент текста экспоната (stage4 §1.2) -- элемент
# GetExhibitContent.Response.chunks. Раньше chunks/chunk_ids были
# параллельными string[]; теперь chunk_id живёт внутри самого элемента,
# и на нём же -- атрибуты непрерываемой речи (guide_robot_semantic_map/
# lib/content_io.py Chunk).

string chunk_id
string text

# false -- автоматический VAD-барж-ин на этом чанке подавляется
# (guide_robot_voice/vad_node.py). Стоп-слово и e-stop не гейтятся этим
# полем -- защита только от непреднамеренного перебоя.
bool interruptible true

# Тишина после этого чанка до следующего, секунды, [0, 15]. Барж-ин во
# время паузы разрешён независимо от interruptible следующего чанка.
float32 pause_after_s 0.0
