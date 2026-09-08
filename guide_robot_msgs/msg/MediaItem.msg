# Один медиа-элемент манифеста экспоната (Task B, B2/B3) -- слайд или
# видео. Отдельная секция от ExhibitChunk/GetExhibitContent -- путь
# нарратива это сообщение не потребляет.

string KIND_IMAGE=image
string KIND_VIDEO=video

string id
string chunk_id      # пусто = медиа уровня экспоната
string kind
string path          # относительный путь от корня медиа, напр. expo_city_model/overview.jpg
float32 duration_s
string caption
