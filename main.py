#!/usr/bin/env python3
import json
import os
import datetime
import re
import sys
import importlib
import locale

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


from server_info import timing_decorator
from database_manager import *

from tracer import TracerManager, TRACER_FILE


__version__ = '0.0.1'
DEBUG = True


try:
    with open('config.json') as config_file:
        _config = json.load(config_file)
    exhibit = str(_config["telegram_token"])
    superuser_id = _config["superuser_id"]
    server_host = _config["server_host"]
    server_username = _config["server_username"]
    server_password = _config["server_password"]
except Exception as e:
    exhibit = None
    print("ОШИБКА при ЧТЕНИИ токена ТЕЛЕГРАМ", e)

bot = Bot(token=exhibit)
dp = Dispatcher(bot, storage=MemoryStorage())


# ================ БАЗА ДАННЫХ И ТАБЛИЦЫ ================
db_manager = DataBaseManager(INSPIRA_DB)
db_manager.create_table(USERS_TABLE_NAME, FIELDS_FOR_USERS)
db_manager.create_table(PRODUCTS_TABLE_NAME, FIELDS_FOR_PRODUCTS)
db_manager.create_table(REFERRALS_TABLE_NAME, FIELDS_FOR_REFERRALS)
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
administrators = Administrators(INSPIRA_DB)


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

    user_manager = UserManager(INSPIRA_DB)
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

        wait_message = await message.answer(
            "<b>➔ SIGNALIZER ⚠️</b>\n"
            "by Kozak Developer\n\n",
            parse_mode='HTML'
        )
        await check_user_data(message)

        check_for_ref = message.text.split(' ')

        if len(check_for_ref) > 1:
            check_for_ref = check_for_ref[1]
            ref_manager = ReferralArrival(INSPIRA_DB)
            ref_manager.check_user_ref(message.from_user.id, check_for_ref)
            print("ID ARRIVAL:", check_for_ref, message.from_user.id)

        await asyncio.sleep(.5)

        if message.from_user.id in administrators.get_list_of_admins():
            kb = [
                [
                    types.KeyboardButton(text="/PANEL/"),
                ]
            ]
            tracer_l.tracer_charge(
                'INFO', message.from_user.id, '/start', "display admin button")
        else:
            kb = [
                [
                    types.KeyboardButton(text="Авторизация"),
                ]
            ]
            pass

        keyboard = types.ReplyKeyboardMarkup(keyboard=kb, resize_keyboard=True)

        await message.answer(
            'Летучка – Сервис для создания тестов при помощи ИИ\n\nhttp://letychka.ru',
            reply_markup=keyboard)

        # try:
        #     await bot.send_photo(
        #         message.from_user.id, photo=InputFile('media/img/menu.png', filename='start_message.png'),
        #         reply_markup=keyboard, parse_mode='HTML',
        #         caption=f'Привет! Здесь ты можешь узнать о готовности своего изделия')
        #     tracer_l.tracer_charge(
        #         'INFO', message.from_user.id, '/start', "user received start message")
        # except Exception as error:
        #     tracer_l.tracer_charge(
        #         'ERROR', message.from_user.id, '/start',
        #         "user failed received start message", f"{error}")
        await wait_message.delete()


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

# -------- ФОРМА ЗАПИСИ НА ЗАНЯТИЕ --------
@dp.message_handler(lambda message: message.text == 'Авторизация')
@dp.message_handler(commands=['registration'])
async def cmd_start(message: types.Message):
    await message.answer("Доступ запрещен")


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
            types.KeyboardButton(text="/GROUPS/"),
            types.KeyboardButton(text="/COMMANDS/"),
            types.KeyboardButton(text="/ADMINS/")
        ],
        [
            types.KeyboardButton(text="/USERS/"),
            types.KeyboardButton(text="/LESSONS/"),
            types.KeyboardButton(text="/PC/")
        ]
    ]


@dp.message_handler(lambda message: message.text == 'signal')
@dp.message_handler(lambda message: message.text == '/PANEL/')
@dp.message_handler(commands=['inspira'])
async def admin_panel(message: types.Message):
    if message.from_user.id in administrators.get_list_of_admins():
        keyboard = types.ReplyKeyboardMarkup(keyboard=ADMIN_PANEL_BUTTONS, resize_keyboard=True)
        await message.reply(
            "[ SIGNALIZER • Admin Panel ]\n\n"
            "<b>Панель администратора</b>\n\n"
            "<i>Здесь Вы можете:\n"
            "• Мониторить статус сервера\n"
            "• Просматривать потребление системных ресурсов", reply_markup=keyboard, parse_mode='HTML')
        tracer_l.tracer_charge(
            'ADMIN', message.from_user.id, admin_panel.__name__, "admin in control panel")
    else:
        print('Enemy')


@dp.message_handler(lambda message: message.text == '/ADMINS/')
async def show_all_admins(message: types.Message):
    if message.from_user.id in administrators.get_list_of_admins():
        await construction_to_delete_messages(message)

        users_id_of_admins = administrators.get_list_of_admins()
        users_man = UserManager(INSPIRA_DB)

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
            "/block <user_id>": "блокировка пользователя по ID",
            "/sms <user_id>": "отправить пользователю сообщение",
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

        user_manager = UserManager(INSPIRA_DB)
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


LOAD_THRESHOLD = 2.0


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
            await send_message_to_telegram(f'{WARNING_SYMBOL} HIGH SERVER LOAD!\n\nСредняя загрузка: {load_average}')

        stdin, stdout, stderr = client.exec_command('echo "Server is reachable"')
        output = stdout.read().decode().strip()

        if output != "Server is reachable":
            await send_message_to_telegram(f'{STOP_SYMBOL} SERVER is DOWN!\n\nOutput: {output}')

        client.close()
    except Exception as e:
        await send_message_to_telegram(f'Ошибка при подключении к серверу: {str(e)}')


@dp.message_handler(commands=['system_info'])
async def cmd_system_info(message: types.Message):
    await construction_to_delete_messages(message)
    try:
        client = paramiko.SSHClient()
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        client.connect(server_host, username=server_username, password=server_password)

        stdin, stdout, stderr = client.exec_command('uname -a && free -h && df -h')
        output = stdout.read().decode()

        sent_message = await message.reply(f'Информация о системе:\n{output}')
        await drop_admin_message(message, sent_message)
    except Exception as e:
        await message.reply(f'Ошибка: {str(e)}')
    finally:
        client.close()


@dp.message_handler(commands=['add_admin'])
async def cmd_add_admin(message: types.Message):
    if message.from_user.id == superuser_id:
        new_admin_id = message.text.split()[1]
        try:
            admin_man = AdminsManager(INSPIRA_DB)
            admin_man.add_new_admin(new_admin_id)
            await message.reply(f"Успешно {CONFIRM_SYMBOL}")
        except Exception as fail:
            await message.reply(f"Неудача {STOP_SYMBOL}\n\n{fail}")


@dp.message_handler(commands=['drop_admin'])
async def cmd_add_admin(message: types.Message):
    if message.from_user.id == superuser_id:
        selected_admin_id = int(message.text.split()[1])

        try:
            admin_man = AdminsManager(INSPIRA_DB)
            admin_man.drop_admin_from_db(selected_admin_id)
            await message.reply("[ OK ] ✅")
        except Exception:
            await message.reply("[ ERROR ] ❌")


limited_users_manager = LimitedUsersManager(INSPIRA_DB)


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

        user_manager = UserManager(INSPIRA_DB)
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

            user_manager = UserManager(INSPIRA_DB)

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

        user_manager = UserManager(INSPIRA_DB)
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
    print(f'===== INSPIRA: {__version__}  =======================================')
    # await general_coroutine()
    tracer_l.tracer_charge(
        "SYSTEM", 0, on_startup.__name__, "start the server")
    scheduler = AsyncIOScheduler()
    scheduler.add_job(check_server_availability, 'interval', minutes=5)
    scheduler.start()
    await bot.send_message(superuser_id, f"{CONFIRM_SYMBOL} SERVER {server_host} is TRACKED")


if __name__ == '__main__':
    try:
        dp.register_message_handler(admin_panel, commands=["signal"])
        executor.start_polling(dp, on_startup=on_startup, skip_updates=True)

    except Exception as critical:
        tracer_l.tracer_charge(
            "CRITICAL", 0, "Exception",
            f"emergency reboot the server", str(critical))
        ServerManager().emergency_reboot()
