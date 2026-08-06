"""Тесты guide_robot_mission_control.

Пустой __init__.py здесь не для красоты: без него каталог `test/` --
namespace-пакет (PEP 420), а у stdlib уже есть *обычный* пакет `test`
(Lib/test/__init__.py) -- обычные пакеты по правилам импорта побеждают
namespace-пакеты независимо от порядка sys.path, так что `import
test.mocks...` резолвился бы в stdlib и падал с "No module named
'test.mocks'". Обычный пакет здесь и обычный пакет там конкурируют уже по
порядку sys.path, и наш каталог оказывается первым.
"""
