string id
string name
uint32 duration_min_estimate
TourStop[] stops
# stage2 блок E: content_id транзитного нарратива (kind=transit в
# guide_robot_semantic_map/content/), пусто -- транзитный нарратив для
# этого тура не звучит. mission_fsm получает Tour только через ListTours
# (нет своего чтения tours.yaml), отсюда и это поле, а не отдельный сервис.
string transit_content_id
