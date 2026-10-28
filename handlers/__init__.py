"""Обработчики сообщений и кнопок.

Новая функция — новый модуль с router = admin_router("имя") (события канала — обычный Router)
и строка в routers(). Порядок важен: сообщение получает первый подошедший обработчик.
"""

from aiogram import Router

from handlers import chat, common, members, music, posts, publish


def routers() -> list[Router]:
    return [
        common.router,   # команды — первыми: /cancel работает в любом состоянии
        chat.router,     # пересланное — раньше фото и видео: фото из личного чата уходит в переписку
        posts.router,    # фото и видео → пост
        music.router,    # трек → видео с музыкой, ввод таймкода
        publish.router,  # публикация, отложка, ввод времени
        members.router,  # подписчики канала
    ]
