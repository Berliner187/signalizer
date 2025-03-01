#!/usr/bin/env python3
import json
import os
import datetime
import re
import sys
import importlib
import locale
import hashlib
import hmac
import time
import secrets


import asyncio
from aiogram import Bot, Dispatcher, types
from aiogram.dispatcher import FSMContext
from aiogram.dispatcher.filters.state import StatesGroup, State
from aiogram.types import InputFile
from aiogram.utils import executor
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.contrib.fsm_storage.memory import MemoryStorage
import paramiko
from apscheduler.schedulers.asyncio import AsyncIOScheduler
import requests
import aiohttp
from Crypto.PublicKey import ECC

from quant import Quant

from server_info import timing_decorator
from database_manager import *

from tracer import TracerManager, TRACER_FILE


__version__ = '2.0.0'
DEBUG = True


try:
    with open('config.json') as config_file:
        _config = json.load(config_file)
    exhibit = str(_config["telegram_token"])
    superuser_id = _config["superuser_id"]
    server_host = _config["server_host"]
    server_username = _config["server_username"]
    server_password = _config["server_password"]
    SECRET_KEY = _config["secret_key"]
except Exception as e:
    exhibit = None
    print("ОШИБКА при ЧТЕНИИ токена ТЕЛЕГРАМ", e)

bot = Bot(token=exhibit)
dp = Dispatcher(bot, storage=MemoryStorage())


# ================ БАЗА ДАННЫХ И ТАБЛИЦЫ ================
db_manager = DataBaseManager(SIGN_DB)
db_manager.create_table(USERS_TABLE_NAME, FIELDS_FOR_USERS)
db_manager.create_table(PRODUCTS_TABLE_NAME, FIELDS_FOR_PRODUCTS)
# db_manager.create_table(REFERRALS_TABLE_NAME, FIELDS_FOR_REFERRALS)
db_manager.create_table(LIMITED_USERS_TABLE_NAME, FIELDS_FOR_LIMITED_USERS)
db_manager.create_table(ADMINS_TABLE_NAME, FIELDS_FOR_ADMINS)

# ============== ИНИЦИАЛИЗАЦИЯ ЛОГИРОВАНИЯ ==========================
tracer_l = TracerManager(TRACER_FILE)

# Локализация
locale.setlocale(locale.LC_TIME, 'ru_RU.UTF-8')


# ===================================================================
# ----------------- ЛИМИТ ЗАПРОСОВ от ПОЛЬЗОВАТЕЛЯ ---------------
user_requests = {}
REQUEST_LIMIT = 12
TIME_LIMIT = 32


notify_banned_users = []


# ===========================
# --- ШАБЛОННЫЕ СООБЩЕНИЯ ---
CONFIRM_SYMBOL = "✅"
WARNING_SYMBOL = "⚠️"
STOP_SYMBOL = "❌"
ADMIN_PREFIX_TEXT = '⚠ CONTROL PANEL ⚠\n'


# Security
temporarily_blocked_users = {}
user_messages = {}


class Administrators(AdminsManager):
    def __init__(self, db_name):
        super().__init__(db_name)

    async def sending_messages_to_admins(self, message: str, parse_mode='HTML', markup=None):
        for _admin_user_id in self.get_administrators_from_db():
            await bot.send_message(_admin_user_id, message, parse_mode=parse_mode, reply_markup=markup)

    def get_list_of_admins(self) -> list:
        return self.get_administrators_from_db()


# Инициализация администраторов
administrators = Administrators(SIGN_DB)


@timing_decorator
async def check_ban_users(user_id):
    # -------------------БАН ЮЗЕРОВ --------------

    check = await check_temporary_block(user_id)
    if check:
        return True

    result = await limited_users_manager.check_user_for_block(user_id)

    if result:
        if user_id not in notify_banned_users:
            await administrators.sending_messages_to_admins(f"⚠ {user_id} VERSUCHT RAUS ZU KOMMEN\n\n")
            await bot.send_message(
                user_id, f"К сожалению, не можем допустить Вас к использованию бота :(\n\n"
                         f"(T_T)", parse_mode='HTML'
            )

            notify_banned_users.append(user_id)
            tracer_l.tracer_charge(
                "ADMIN", user_id, "check_ban_users", "VERSUCHT RAUS ZU KOMMEN")
        return True


async def block_user_temporarily(user_id):
    temporarily_blocked_users[user_id] = datetime.datetime.now() + datetime.timedelta(minutes=30)
    await bot.send_message(
        user_id,
        f"К сожалению, не можем допустить Вас к использованию бота :(\n\n{temporarily_blocked_users[user_id]}", parse_mode='HTML')


async def check_temporary_block(user_id):
    if user_id in temporarily_blocked_users:
        if datetime.datetime.now() > temporarily_blocked_users[user_id]:
            del temporarily_blocked_users[user_id]
            return False
        else:
            tracer_l.tracer_charge(
                'ADMIN', user_id, check_temporary_block.__name__, "user will temp banned")
            return True
    else:
        return False


@timing_decorator
async def ban_request_restrictions(user_id):
    current_time = time()

    if user_id not in user_messages:
        user_messages[user_id] = []

    user_messages[user_id] = [t for t in user_messages[user_id] if current_time - t <= 30]
    user_messages[user_id].append(current_time)

    if len(user_messages[user_id]) >= REQUEST_LIMIT:
        if len(user_messages[user_id]) == TIME_LIMIT:
            await limited_users_manager.block_user(f"/ban {user_id}")
            await administrators.sending_messages_to_admins(f"ЛИКВИДИРОВАН ❌")
            tracer_l.tracer_charge(
                'ADMIN', user_id, ban_request_restrictions.__name__, "user will permanent banned")

        if await check_temporary_block(user_id) is False:
            await block_user_temporarily(user_id)
            user_messages[user_id] = []


@timing_decorator
async def check_user_data(message):
    user_id = message.from_user.id
    first_name = message.chat.first_name
    last_name = message.chat.last_name

    user_manager = UserManager(SIGN_DB)
    result = user_manager.check_user_in_database(user_id)

    if not result:
        _time_now = datetime.datetime.now().strftime('%H:%M %d-%m-%Y')
        user_data = {
            'user_id': message.from_user.id, 'fullname': message.chat.first_name,
            'date_register': _time_now, 'user_status': True,
            'user_status_date_upd': _time_now
        }
        user_manager.add_record('users', user_data)

        await administrators.sending_messages_to_admins(
            f"⚠ НОВЫЙ ГОСТЬ ⚠\n{first_name} {last_name} ({user_id})")

        tracer_l.tracer_charge(
            'ADMIN', message.from_user.id, check_user_data.__name__, "new user")

    return result


@dp.callback_query_handler(lambda c: c.data == 'close_session')
async def process_close_session(callback_query: types.CallbackQuery):
    user_id = callback_query.from_user.id
    await send_close_session_request(user_id)
    await bot.answer_callback_query(callback_query.id, text="Запрос на закрытие сессии отправлен.")


def generate_hash(user_id):
    message = json.dumps({'user_id': user_id}).encode()
    return hmac.new(SECRET_KEY.encode(), message, hashlib.sha256).hexdigest()


async def send_close_session_request(user_id):
    api_url = "https://letychka.ru/api/ghost_disconnect/"
    gen_hash = generate_hash(user_id)
    payload = {
        'user_id': user_id,
        'hash': gen_hash
    }

    async with aiohttp.ClientSession() as session:
        try:
            async with session.post(api_url, json=payload) as response:
                # Проверяем статус ответа
                if response.status == 200:
                    print('Post Terminate Session')
                    tracer_l.tracer_charge(
                        'INFO', user_id, send_close_session_request.__name__, 'Post Terminate Session')
                else:
                    print('Fail Terminate Session')
                    print(response)
                    try:
                        response_data = await response.json()
                    except aiohttp.ContentTypeError:
                        response_data = await response.text()
                    tracer_l.tracer_charge(
                        'ERROR', user_id, send_close_session_request.__name__,
                        'Fail in Terminate Session', response_data)
        except Exception as e:
            print(f'Exception occurred: {e}')
            tracer_l.tracer_charge(
                'ERROR', user_id, send_close_session_request.__name__,
                'Exception in Terminate Session', str(e))


def generate_auth_token(telegram_user_id: int) -> tuple:
    """
        Генерирует токен и хэш для одноразовой ссылки.
        Возвращает (токен, хэш).
    """
    timestamp = str(int(time()))
    random_part = secrets.token_hex(16)
    token = f"{telegram_user_id}:{timestamp}:{random_part}"
    token_hash = hashlib.sha256(token.encode()).hexdigest()
    return token, token_hash


temp_messages = {}


# ============================================================================
# ------------------------- ПОЛЬЗОВАТЕЛЬСКИЕ КОМАНДЫ -------------------------
@dp.message_handler(text='Запуск')
@dp.message_handler(text='Старт')
@dp.message_handler(text='Начать')
@dp.message_handler(commands=['start'])
async def start_message(message: types.Message):
    if await check_ban_users(message.from_user.id) is not True:
        tracer_l.tracer_charge(
            'INFO', message.from_user.id, start_message.__name__, "user launched bot")

        if not await check_user_data(message):
            wait_message = await message.answer(
                "<b>➔ ЛЕТУЧКА ⚠️</b>\n\n"
                "Design by Kozak Developer\n\n",
                parse_mode='HTML'
            )

        check_for_ref = message.text.split(' ')

        if len(check_for_ref) > 1:
            check_for_ref = check_for_ref[1]
            if str(check_for_ref).startswith('login'):
                kb = [
                    [
                        'Подтвердить номер телефона'
                    ]
                ]
                keyboard = types.ReplyKeyboardMarkup(keyboard=kb, resize_keyboard=True)

                await message.answer(
                    'Пожалуйста, подтвердите номер телефона, чтобы иметь возможность входить на платформу',
                    reply_markup=keyboard)
                # ref_manager = ReferralArrival(SIGN_DB)
                # ref_manager.check_user_ref(message.from_user.id, check_for_ref)
                print("ID ARRIVAL:", check_for_ref, message.from_user.id)
        else:
            # keyboard = types.ReplyKeyboardMarkup(keyboard=kb, resize_keyboard=True)

            # await message.answer(
            #     'Летучка – Сервис для создания тестов при помощи ИИ\n\nhttp://letychka.ru',
            #     reply_markup=keyboard)

            user_id = message.from_user.id

            try:
                if not await check_user_data(message):
                    await bot.send_photo(
                        message.from_user.id,
                        photo=InputFile('media/img/letychka-robot-start.png', filename='start_message.png'),
                        parse_mode='HTML',
                        caption=f'<b>Летучка – Создавайте тесты при помощи нейросети.</b>\n\n'
                                f'Загружайте свои учебные материалы для создания персонализированных тестов!\n\n'
                                f'<b>Преимущества:</b>\n'
                                f'• Возможность загрузки файлов с материалами\n'
                                f'• Проходить тесты можно сразу на платформе\n'
                                f'• Персонализированная обратная связь от ИИ, с указанием на слабые и сильные стороны\n'
                                f'• Возможность скачивать сгенерированные тесты в PDF\n\n'
                                f'Карточка сервиса ☞ <a href="https://productradar.ru/product/letuchka/">Летучка</a>\n\n'
                                f'<a href="https://t.me/LetychkaRobot">@LetychkaRobot</a>')

                entry_url_mes = await bot.send_message(message.from_user.id, "Формируем ссылку для входа...")

                token, token_hash = generate_auth_token(user_id)
                auth_url = f"https://letychka.ru/api/v2/one_click_auth/{token}/{token_hash}/"

                keyboard = types.InlineKeyboardMarkup()
                button = types.InlineKeyboardButton(text="Открыть Летучку", url=f"{auth_url}")
                keyboard.add(button)

                await asyncio.sleep(.5)
                await bot.send_message(
                    message.from_user.id,
                    "Нажмите на кнопку ниже, чтобы перейти на платформу.\n\n<i>Кнопка для входа действует 5 минут и действительна единожды</i>",
                    reply_markup=keyboard, parse_mode='HTML')

                tracer_l.tracer_charge(
                    'INFO', message.from_user.id, '/start', "user received start message")

                await entry_url_mes.delete()

            except Exception as error:
                print("user failed received start message", f"{error}")
                tracer_l.tracer_charge(
                    'ERROR', message.from_user.id, '/start',
                    "user failed received start message", f"{error}")
        try:
            await wait_message.delete()
        except Exception as fail:
            print('pass', fail)


@dp.message_handler(commands=['help'])
async def help_user(message: types.Message):
    # =========== ПРОВЕРКА ДОПУСКА ПОЛЬЗОВАТЕЛЯ ================
    if await check_ban_users(message.from_user.id) is not True:
        tracer_l.tracer_charge(
            'INFO', message.from_user.id, help_user.__name__, "user in help")

        url_kb = InlineKeyboardMarkup(row_width=2)
        url_help = InlineKeyboardButton(text='Поддержка', url='https://google.com')
        url_link = InlineKeyboardButton(text='Наш сайт', url='https://google.com')
        url_kb.add(url_help, url_link)
        await message.answer(
            'Если возникли какие-либо трудности или вопросы, пожалуйста, ознакомьтесь со списком ниже',
            reply_markup=url_kb)


# =============================================================================
# --------------------------- НАВИГАЦИЯ ---------------------------------------
# --------------------- ДЛЯ ВСЕХ ПОЛЬЗОВАТЕЛЕЙ --------------------------------
@dp.message_handler(lambda message: message.text == 'Авторизация')
@dp.message_handler(lambda message: message.text == 'Подтвердить номер телефона')
@dp.message_handler(commands=['registration'])
async def get_contact_info(message: types.Message):
    user_manager = UserManager(SIGN_DB)

    user_phone_number = user_manager.get_phone(user_id=message.from_user.id)

    if user_phone_number:
        await message.answer("Номер уже зарегистрирован")
    # else:
    keyboard = types.ReplyKeyboardMarkup(resize_keyboard=True, one_time_keyboard=True)
    phone_button = types.KeyboardButton(text="📱 Отправить номер телефона", request_contact=True)
    keyboard.add(phone_button)
    tracer_l.tracer_charge(
        'INFO', message.from_user.id, get_contact_info.__name__, "offer to send a contact")
    await message.answer(
        "Пожалуйста, отправьте свой номер телефона, чтобы подтвердить свой аккаунт 👇", reply_markup=keyboard)


def hash_data(data):
    data_string = json.dumps(data, sort_keys=True).encode()
    return hashlib.sha256(data_string).hexdigest()


@dp.message_handler(content_types=types.ContentType.CONTACT)
async def contact_handler(message: types.Message):
    user_id = message.from_user.id
    phone = message.contact.phone_number

    try:
        temp_mes = await message.answer("Обмениваемся секретами... 🤝🔑")

        user_manager = UserManager(SIGN_DB)
        user_manager.update_contact_info(user_id=user_id, phone=phone)

        data = {
            'telegram_user_id': user_id,
            'phone_number': phone,
            'username': message.from_user.username,
            'first_name': message.from_user.first_name,
            'last_name': message.from_user.last_name,
        }

        data_hash = hash_data(data)

        crypto = Quant()
        crypto.generate_keys_with_secret()

        public_key_pem = crypto.public_key.export_key(format='PEM')

        exchange_response = requests.post(
            'https://letychka.ru/api/v2/signal-secure/exchange_keys/',
            json={
                'public_key': public_key_pem,
                'telegram_user_id': user_id
            }
        )

        if exchange_response.status_code != 200:
            print("exchange_response.status_code", exchange_response.status_code)
            raise ValueError("Ошибка при обмене ключами")

        server_public_key_pem = exchange_response.json().get("public_key")
        if not server_public_key_pem:
            raise ValueError("Не удалось получить публичный ключ сервера")

        server_public_key = ECC.import_key(server_public_key_pem)

        crypto.derive_shared_key(server_public_key)

        data_bytes = json.dumps(data).encode('utf-8')

        nonce, ciphertext, tag = crypto.encrypt_data(data_bytes)

        response = requests.post('https://letychka.ru/api/v2/signal-secure/', json={
            'nonce': nonce.hex(),
            'ciphertext': ciphertext.hex(),
            'tag': tag.hex(),
            'data_hash': data_hash,
            'telegram_user_id': user_id
        })

        await asyncio.sleep(.5)
        await temp_mes.delete()
        temp_mes = await message.answer("Момент... 🔐")

        user_id = message.from_user.id

        token, token_hash = generate_auth_token(user_id)
        auth_url = f"https://letychka.ru/api/v2/one_click_auth/{token}/{token_hash}/"

        keyboard = types.InlineKeyboardMarkup()
        button = types.InlineKeyboardButton(text="Открыть Летучку", url=f"{auth_url}")
        keyboard.add(button)

        if response.status_code == 200:
            await asyncio.sleep(.5)
            tracer_l.tracer_charge(
                'INFO', user_id, contact_handler.__name__, "send a conf data by API")

            await message.answer(
                f"<b>Готово!</b> 🎉\n\nМожете входить по своему номеру телефона!\n\n<i>Перейдите по ссылке ниже, чтобы войти</i>",
                reply_markup=keyboard, parse_mode='HTML')
        else:
            try:
                error_data = response.json()
                error_message = error_data.get('message', 'Неизвестная ошибка')
            except json.JSONDecodeError:
                error_message = response.text

            tracer_l.tracer_charge(
                'ERROR', user_id, contact_handler.__name__, f"send a conf data by API: {error_message}")

            await message.answer(f"Ошибка: {error_message}")
        await temp_mes.delete()

    except Exception as e:
        print(f"Error: {e}")
        tracer_l.tracer_charge(
            'ERROR', user_id, contact_handler.__name__, f"Error: {str(e)}")
        await message.answer("Произошла ошибка. Пожалуйста, попробуйте позже.")


# ==========================================================================
# --------------------------- АДМИНАМ --------------------------------------
last_admin_message_id, last_admin_menu_message_id = {}, {}


# ----- МЕХАНИЗМ УДАЛЕНИЯ СООБЩЕНИЯ (ИМИТАЦИЯ МЕНЮ для АДМИНА)
async def construction_to_delete_messages(message):
    try:
        if last_admin_message_id.get(message.from_user.id):
            await bot.delete_message(message.chat.id, last_admin_message_id[message.from_user.id])
        if last_admin_menu_message_id.get(message.from_user.id):
            await bot.delete_message(message.chat.id, last_admin_menu_message_id[message.from_user.id])
    except Exception:
        pass


async def drop_admin_message(message: types.Message, sent_message):
    last_admin_message_id[message.from_user.id] = sent_message.message_id
    last_admin_menu_message_id[message.from_user.id] = message.message_id


# Кнопки на админ-панели
ADMIN_PANEL_BUTTONS = [
        [
            types.KeyboardButton(text="/SYSTEM_CHECK/"),
            types.KeyboardButton(text="/COMMANDS/"),
            types.KeyboardButton(text="/ADMINS/")
        ],
        [
            types.KeyboardButton(text="/USERS/"),
            types.KeyboardButton(text="/PC/")
        ]
    ]


@dp.message_handler(lambda message: message.text == 'signal')
@dp.message_handler(lambda message: message.text == '/PANEL/')
@dp.message_handler(commands=['signal'])
async def admin_panel(message: types.Message):
    if message.from_user.id in administrators.get_list_of_admins():
        keyboard = types.ReplyKeyboardMarkup(keyboard=ADMIN_PANEL_BUTTONS, resize_keyboard=True)
        await message.reply(
            "[ SIGNALIZER • Admin Panel ]\n\n"
            "<b>Панель администратора</b>\n\n"
            "<i>Здесь Вы можете:\n"
            "• Мониторить статус сервера\n"
            "• Просматривать потребление системных ресурсов</i>", reply_markup=keyboard, parse_mode='HTML')
        tracer_l.tracer_charge(
            'ADMIN', message.from_user.id, admin_panel.__name__, "admin in control panel")
    else:
        print('Enemy')


@dp.message_handler(lambda message: message.text == '/ADMINS/')
async def show_all_admins(message: types.Message):
    if message.from_user.id in administrators.get_list_of_admins():
        await construction_to_delete_messages(message)

        users_id_of_admins = administrators.get_list_of_admins()
        users_man = UserManager(SIGN_DB)

        markup = InlineKeyboardMarkup()

        for admin_id in users_id_of_admins:
            user_data = users_man.get_user_data(admin_id)
            try:
                first_name = user_data[2]
                phone_number = user_data[3]
            except TypeError:
                first_name = '-'
                phone_number = '-'
            button = InlineKeyboardButton(f"{first_name} • {phone_number}", callback_data=f"admin_card:{admin_id}")
            markup.add(button)

        _sent_message = await bot.send_message(
            message.from_user.id,
            f"{ADMIN_PREFIX_TEXT}СПИСОК ВСЕХ АДМИНИСТРАТОРОВ", reply_markup=markup, parse_mode='HTML')

        await drop_admin_message(message, _sent_message)


@dp.message_handler(lambda message: message.text == '/COMMANDS/')
async def show_all_commands(message: types.Message):
    if message.from_user.id in administrators.get_list_of_admins():
        await construction_to_delete_messages(message)

        dict_commands = {
            "/signal": "панель администратора",
            "/system_info": "информация о ресурсах сервера",
            "/block user_id": "блокировка пользователя по ID",
            "/sms user_id": "отправить пользователю сообщение",
            "/limited_users": "просмотреть список заблокированных пользователей",
        }

        commands_to_out = ""
        for command, disc in dict_commands.items():
            commands_to_out += f"{command} – {disc}\n"

        _sent_message = await bot.send_message(
            message.from_user.id, f"{ADMIN_PREFIX_TEXT}{commands_to_out}")

        await drop_admin_message(message, _sent_message)


@dp.message_handler(lambda message: message.text == '/USERS/')
async def show_all_users(message: types.Message):
    if message.from_user.id in administrators.get_list_of_admins():
        wait_message = await message.answer("➜ LOADING DB... ///")
        await construction_to_delete_messages(message)

        user_manager = UserManager(SIGN_DB)
        all_users = user_manager.read_users_from_db()

        users_from_db = '➜ LAST USERS ➜\n\n'
        users_from_db_count = 0

        cnt_users = len(all_users)

        date_format = "%H:%M %d-%m-%Y"
        sorted_users = reversed(sorted(all_users, key=lambda x: datetime.datetime.strptime(x[5], date_format)))

        for user in sorted_users:
            id_in_db = user[0]
            user_id = user[1]
            firstname = user[2]
            username = user[4]
            date = user[5]

            users_from_db += f"[{id_in_db}]: ({str(date).split(' ')[1]}) {firstname}\n{user_id}\n"
            users_from_db_count += 1

            if users_from_db_count >= 20:
                users_from_db += f'... и еще {cnt_users - 20}\n'
                users_from_db += f'[ADMIN] ' \
                                 f'{sorted(all_users, key=lambda x: datetime.datetime.strptime(x[5], date_format))[0][1]}'
                break

        users_from_db += f"\n\n<b>➜ TOTAL {cnt_users}</b>"

        await wait_message.delete()
        sent_message = await message.answer(users_from_db, parse_mode="HTML")
        await drop_admin_message(message, sent_message)


@dp.message_handler(lambda message: message.text == '/PC/')
async def monitor_process(message: types.Message):
    if message.from_user.id in administrators.get_list_of_admins():
        await construction_to_delete_messages(message)
        try:
            from server_info import MachineResources

            machine_resources = MachineResources()
            sent_message = await message.answer(machine_resources.get_all_info())

            await drop_admin_message(message, sent_message)
        except ModuleNotFoundError as e:
            await message.answer("Ошибка импортирования модуля", e)


async def send_message_to_telegram(message):
    await bot.send_message(superuser_id, message)


LOAD_THRESHOLD = 50.0


async def check_server_availability():
    try:
        client = paramiko.SSHClient()
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        client.connect(server_host, username=server_username, password=server_password)

        stdin, stdout, stderr = client.exec_command('uptime')
        output = stdout.read().decode().strip()

        load_average = output.split('load average: ')[1].split(',')[0]
        load_average = float(load_average)

        if load_average > LOAD_THRESHOLD:
            await send_message_to_telegram(
                f'{WARNING_SYMBOL} HIGH SERVER LOAD!\n\n'
                f'{get_format_date()}\n'
                f'В среднем – {load_average}')

        stdin, stdout, stderr = client.exec_command('echo "Server is reachable"')
        output = stdout.read().decode().strip()

        if output != "Server is reachable":
            await send_message_to_telegram(f'{STOP_SYMBOL} SERVER is DOWN!\n\nOutput: {output}')

        client.close()
    except Exception as e:
        await send_message_to_telegram(f'Ошибка при подключении к серверу: {str(e)}')


@dp.message_handler(commands=['system_info'])
async def cmd_system_info(message: types.Message):
    if message.from_user.id in administrators.get_list_of_admins():
        try:
            client = paramiko.SSHClient()
            client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
            client.connect(server_host, username=server_username, password=server_password)

            commands = [
                'uname -a',
                'uptime',
                'free -h',
                'df -h',
                'ss -tuln'
                'ping google.com',
                'id',
            ]

            output = ""
            for command in commands:
                stdin, stdout, stderr = client.exec_command(command)
                output += f'Команда: {command}\n{stdout.read().decode()}\n{"-"*40}\n'

            await send_long_message(message.chat.id, f'Информация о системе:\n{output}')
        except Exception as e:
            await send_long_message(message.chat.id, f'Ошибка: {str(e)}')
        finally:
            client.close()


async def send_long_message(chat_id, text, max_length=1024):
    for i in range(0, len(text), max_length):
        await bot.send_message(chat_id, text[i:i + max_length])


@dp.message_handler(commands=['add_admin'])
async def cmd_add_admin(message: types.Message):
    if message.from_user.id == superuser_id:
        new_admin_id = message.text.split()[1]
        try:
            admin_man = AdminsManager(SIGN_DB)
            admin_man.add_new_admin(new_admin_id, "0")
            await message.reply(f"Успешно {CONFIRM_SYMBOL}")
        except Exception as fail:
            await message.reply(f"Неудача {STOP_SYMBOL}\n\n{fail}")


@dp.message_handler(commands=['drop_admin'])
async def cmd_add_admin(message: types.Message):
    if message.from_user.id == superuser_id:
        selected_admin_id = int(message.text.split()[1])

        try:
            admin_man = AdminsManager(SIGN_DB)
            admin_man.drop_admin_from_db(selected_admin_id)
            await message.reply("[ OK ] ✅")
        except Exception:
            await message.reply("[ ERROR ] ❌")


limited_users_manager = LimitedUsersManager(SIGN_DB)


@dp.message_handler(commands=['limited_users'])
async def blacklist_cat_users(message: types.Message):
    if message.from_user.id in administrators.get_list_of_admins():
        await construction_to_delete_messages(message)

        blocked_users = await limited_users_manager.fetch_all_limited_users()
        sent_message = await message.answer(blocked_users)

        await drop_admin_message(message, sent_message)


@dp.message_handler(commands=['block'])
async def block_user(message: types.Message):
    if message.from_user.id in administrators.get_list_of_admins():
        await construction_to_delete_messages(message)

        try:
            answer = await limited_users_manager.block_user(message.text)
            sent_message = await message.reply(f"<b>{answer}</b>", parse_mode='HTML')
            tracer_l.tracer_charge(
                "ADMIN", message.from_user.id, block_user.__name__, f"user success blocked")

        except sqlite3.IntegrityError:
            sent_message = await message.reply(f"<b>ALREADY BLOCKED 🟧</b>", parse_mode='HTML')
            tracer_l.tracer_charge(
                "ADMIN", message.from_user.id, block_user.__name__, f"user already blocked")

        except Exception as error:
            sent_message = await message.reply("/// ERROR:", error)
            tracer_l.tracer_charge(
                "ADMIN", message.from_user.id, block_user.__name__,
                f"error while trying blocked user", f"{error}")

        await drop_admin_message(message, sent_message)


@dp.message_handler(commands=['unblock'])
async def unblock_user(message: types.Message):
    if message.from_user.id in administrators.get_list_of_admins():
        await construction_to_delete_messages(message)
        try:
            answer = await limited_users_manager.unblock_user(message.text)
            sent_message = await message.reply(f"<b>{answer}</b>", parse_mode='HTML')
        except Exception as e:
            sent_message = await message.reply("/// ERROR:", e)
        await drop_admin_message(message, sent_message)


@dp.message_handler(commands=['i'])
async def req_in_db(message: types.Message):
    if message.from_user.id in administrators.get_list_of_admins():
        await construction_to_delete_messages(message)
        _user_id = int(message.text.split()[1])

        user_manager = UserManager(SIGN_DB)
        _user_card = user_manager.get_user_card(_user_id, 'user')

        try:
            status_user_in_bot = await limited_users_manager.check_user_for_block(_user_id)
        except OverflowError as overflow:
            await message.answer(f"➜ ERROR ➜\n\n{overflow}")
            return

        if _user_card:
            if status_user_in_bot:
                text_status_user_in_bot = '➜ (ЛИКВИДИРОВАН ❌)'
            else:
                text_status_user_in_bot = ''
            sent_message = await message.answer(
                f"➜ Карточка пользователя ➜\n\n"
                f"{_user_card}\n\n{text_status_user_in_bot}", parse_mode='HTML')
        else:
            sent_message = await message.answer(f"➜ USER not exist ❌")

        await drop_admin_message(message, sent_message)


@dp.message_handler(commands=['drop'])
async def req_in_db(message: types.Message):
    if message.from_user.id in administrators.get_list_of_admins():
        try:
            _user_id = int(message.text.split()[1])

            user_manager = UserManager(SIGN_DB)

            try:
                user_manager.drop_user_from_db(_user_id)
                await message.answer("<b>DROP USER: OK ✅</b>", parse_mode='HTML')
            except Exception as e:
                await message.answer(f"<b>DROP USER: ERROR ❌</b>\n\n{e}", parse_mode='HTML')

        except Exception:
            await message.reply("Неверно переданы аргументы.")


@dp.message_handler(commands=['sms'])
async def send_html_message(message: types.Message):
    """
        Отправка сообщения пользователю по user_id, с HTML-форматированием
    """
    if message.from_user.id in administrators.get_list_of_admins():
        try:
            adv_text = len(message.text.split())
            if adv_text > 2:
                _message = ' '.join(message.text.split()[2:])
            else:
                _message = message.text.split()[2]
            _message = _message.replace("\\n", "\n")

            try:
                await bot.send_message(chat_id=message.text.split()[1], text=_message, parse_mode="HTML")
                await message.answer("<b>ДОСТАВЛЕНО ✅</b>", parse_mode='HTML')
            except Exception as e:
                print(e)
                await message.answer("<b>НЕ УДАЛОСЬ ❌</b>", parse_mode='HTML')
        except Exception:
            await message.reply("Неверно переданы аргументы.")


@dp.message_handler(commands=['all'])
async def sent_message_to_user(message: types.Message):
    if message.from_user.id in administrators.get_list_of_admins():
        keyboard = types.ReplyKeyboardMarkup(keyboard=ADMIN_PANEL_BUTTONS, resize_keyboard=True)

        # try:
        split_cnt = len(message.text.split())
        if split_cnt > 2:
            _message = ' '.join(message.text.split()[1:])
        else:
            _message = message.text.split()[1]
        _message = _message.replace("\\n", "\n")

        user_manager = UserManager(SIGN_DB)
        users_load = user_manager.read_users_from_db()

        cnt_users = 0
        cnt_er = 0
        count = 0

        import time

        sent_mes = await bot.send_message(superuser_id, "➜ <b>SENDING STREAM MESSAGES</b> ... [wait]", parse_mode='HTML')
        start_time = time.time()

        for row in users_load:
            count += 1
            _user_id = row[1]

            try:
                status_user_in_bot = await limited_users_manager.check_user_for_block(_user_id)

                if (cnt_users % 15 == 0) and (cnt_users != 0):
                    print('sleep 5:', cnt_users, len(users_load), count)
                    await asyncio.sleep(5)
                else:
                    print('sleep 0.25:', cnt_users, len(users_load), count)
                    await asyncio.sleep(.25)

                if status_user_in_bot:
                    print('pass user', count)
                else:
                    await bot.send_message(chat_id=_user_id, text=_message, parse_mode="HTML")
                    cnt_users += 1

            except Exception:
                cnt_er += 1

        end_time = time.time()
        execution_time = round(end_time - start_time)

        hours = int(execution_time // 3600)
        minutes = int((execution_time % 3600) // 60)
        seconds = int(execution_time % 60)
        final_people_text_execution_time = f'{hours} h, {minutes} m, {seconds} s'

        await sent_mes.delete()
        await bot.send_message(
            superuser_id, f"➜ DONE {cnt_users}\n➜ NOT COMPLETED {cnt_er}\n\n"
                          f"➜ TIMING - {final_people_text_execution_time}",
            reply_markup=keyboard)


async def general_coroutine():
    print("\nSTART the COROUTINE: [ OK ]\n")
    while True:
        now = datetime.datetime.now()

        if now.hour == 12 and now.minute == 0:
            for admin_id in administrators.get_list_of_admins():
                await bot.send_message(admin_id, "Статистика за день: ...")
            await asyncio.sleep(60)
        if now.hour == 17 and now.minute == 2:
            for admin_id in administrators.get_list_of_admins():
                await bot.send_message(admin_id, "Test: test ...")

        await asyncio.sleep(30)


# ------------- АДМИНИСТРИРОВАНИЕ СЕРВЕРНОЙ ЧАСТИ -----------
@dp.message_handler(commands=['reboot'])
async def reboot_server(message: types.Message):
    if message.from_user.id == superuser_id:
        await message.reply("➜ REBOOT in 5 sec... ➜")
        tracer_l.tracer_charge(
            "WARNING", message.from_user.id, reboot_server.__name__,
            f"{message.from_user.id} reboot the server")
        await asyncio.sleep(5)
        await ServerManager().emergency_reboot()
    else:
        tracer_l.tracer_charge(
            "WARNING", message.from_user.id, reboot_server.__name__,
            f"{message.from_user.id} try to reboot the server")


class ServerManager:
    @staticmethod
    def __reboot_server():
        os.execl(sys.executable, sys.executable, *sys.argv)

    async def emergency_reboot(self):
        print("emergency_reboot: start")
        self.__reboot_server()
# ==========================================================================


# ==========================================================================
# ------------------------ ТАБЛО СЕРВЕРНОЙ ЧАСТИ ---------------------------
async def on_startup(dp):
    os.system('clear')
    print('==================== BOT SIGNAL START ========================')
    print(
    """
    SIGNAL SIGNAL SIGNAL SIGNAL SIGNAL SIGNAL SIGNAL
    SIGNAL SIGNAL SIGNAL SIGNAL SIGNAL SIGNAL SIGNAL
    SIGNAL SIGNAL SIGNAL SIGNAL SIGNAL SIGNAL SIGNAL
    """
    )
    print(f'===== DEBUG: {DEBUG} =============================================')
    print(f'===== SIGNAL: {__version__}  =======================================')
    # await general_coroutine()
    tracer_l.tracer_charge(
        "SYSTEM", 0, on_startup.__name__, "start the server")
    scheduler = AsyncIOScheduler()
    scheduler.add_job(check_server_availability, 'interval', minutes=5)
    scheduler.start()
    await bot.send_message(superuser_id, f"{CONFIRM_SYMBOL} SERVER {server_host} is TRACKED")


if __name__ == '__main__':
    dp.register_message_handler(admin_panel, commands=["signal"])
    executor.start_polling(dp, on_startup=on_startup, skip_updates=True)
