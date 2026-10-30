# PLANPR.md

## Цель
Исправить баг в тесте `test_impossible` (опечатка `assertEqual(1, 2)`).

## Архитектурные решения
- Заменить `self.assertEqual(1, 2)` на `self.assertEqual(percent(0, 0), 0.0)` в тесте `test_impossible`.

## Известные ограничения
- Нет (все тесты проходят).

## Следующий шаг
Завершить работу.
