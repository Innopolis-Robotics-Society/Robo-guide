"""Тесты guide_robot_llm.

Пустой __init__.py здесь не для красоты: без него каталог `test/` --
namespace-пакет (PEP 420), а у stdlib уже есть *обычный* пакет `test`
(Lib/test/__init__.py), который по правилам импорта побеждает
namespace-пакеты -- `import test.mocks...` резолвился бы в stdlib и падал
с "No module named 'test.mocks'" (см. тот же приём в
guide_robot_mission_control/test/__init__.py).
"""
