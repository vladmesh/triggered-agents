# curator

Репо куратора памяти. Часть субстрата секретаря (control-panel).

- Куратор = единственный writer канона (`panelmem-kb`). Агенты только читают.
- Пишет markdown в канон + обновляет `baseline.md`. Индекс ребилдит memory-mcp, не куратор.
- Дизайн и инварианты — `README.md` здесь и `control-panel/docs/secretary.md`.
- Не коммитишь/не пушишь без явной просьбы.
