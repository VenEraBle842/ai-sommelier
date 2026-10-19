Файлы `merlo.html`, `shenen.html`, `blend.html` и `catalog.html` — сокращённые
фрагменты реального HTML, загруженного 19 октября 2026 года с адресов:

- https://vino-svoe.ru/wines/merlo-drozd
- https://vino-svoe.ru/wines/shenen-blan-beton-drozd
- https://vino-svoe.ru/wines/suhoe-beloe-traminer-pino-blan-sh-ardone
- https://vino-svoe.ru/wines

Удалены служебные атрибуты, стили, скрипты, навигация и блоки похожих вин.
Классы элементов и значения полей сохранены. Фрагменты нужны для автономных
тестов парсера и не загружаются в рабочую базу при запуске приложения.

`incomplete.html` и `malformed.html` — искусственные случаи для тестов.
Любая другая искусственная разметка в тестах также служит только проверке
ошибок и граничных условий, а не наполнению рабочего каталога.
