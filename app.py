#!/usr/bin/env python
# -*- coding: utf-8 -*-
import datetime
import hashlib
import json
import os
import random
import re
import sys

import requests
import telebot
import xmltodict
from flask import Flask, request, render_template, jsonify, session, redirect, url_for
from telebot.apihelper import ApiTelegramException

import core
import schedule_updater
import settings
from settings import KEYBOARD

app = Flask(__name__, template_folder='site', static_folder='site/static', static_url_path='/fl/static')
app.secret_key = hashlib.md5(settings.ADMIN_PASSWORD.encode('utf-8')).hexdigest()

bot = telebot.TeleBot(settings.BOT_TOKEN, threaded=True)

keyboard = telebot.types.ReplyKeyboardMarkup(resize_keyboard=True, one_time_keyboard=True)
keyboard.row(KEYBOARD['TODAY'], KEYBOARD['TOMORROW'], KEYBOARD['FOR_A_WEEK'])
keyboard.row(KEYBOARD['FOR_A_TEACHER'], KEYBOARD['FOR_A_GROUP'], KEYBOARD['HELP'])

emoji_numbers = ['0⃣', '1⃣', '2⃣', '3⃣', '4⃣', '5⃣', '6⃣', '7⃣', '8⃣', '9⃣']


def get_timetable(teacher='', group='', sdate='', edate='', user_id=None):

    try:

        raw_group = group

        def get_teachers_last_name_and_initials(full_name):

            if len(full_name.split()) == 3:
                last_name, first_name, third_name = full_name.split()

                return f'{last_name} {first_name[0]}.{third_name[0]}.'

        def get_valid_case_group(group):

            with open(os.path.join(settings.BASE_DIR, 'data', 'valid_case_groups.json'), 'r', encoding="utf-8") as file:
                all_groups = json.loads(file.read())

            for possible_group in all_groups:
                if group.lower() == possible_group.lower():
                    return possible_group

        group = get_valid_case_group(raw_group)

        if not group and not teacher:
            return []

        http_headers = {
            'User-Agent': settings.HTTP_USER_AGENT,
        }

        request_params = {
            'req_type': 'rozklad',
            'dep_name': '',
            'OBJ_ID': '',
            'ros_text': 'united',
            'begin_date': sdate,
            'end_date': edate,
            'req_format': 'xml',
            'coding_mode': 'WINDOWS-1251',
            'bs': 'ok',
            'show_empty': 'no',
        }

        if group:
            request_params['req_mode'] = 'group'
            request_params['OBJ_name'] = group.encode('windows-1251')
        else:
            request_params['req_mode'] = 'teacher'
            teacher = get_teachers_last_name_and_initials(teacher)
            request_params['OBJ_name'] = teacher.encode('windows-1251')

        response = requests.get(settings.API_LINK, params=request_params, headers=http_headers, timeout=45)

        timetable = xmltodict.parse(response.text)

        if 'psrozklad_export' in timetable:
            timetable = timetable.get('psrozklad_export')

        if not timetable.get('roz_items'):
            return []

        d = {}

        def get_day_name_by_date(str_date):

            day, month, year = map(int, str_date.split('.'))
            date = datetime.date(year, month, day)

            day_in_week_number = date.isoweekday()

            day_names = ['Понеділок', 'Вівторок', 'Середа', 'Четвер', 'П\'ятниця', 'Субота', 'Неділя']

            return day_names[day_in_week_number - 1]

        def clear_text(text):

            if text:
                # text = text.replace('<br> ', '\n', text.count('<br>') - 1)
                text = text.replace('<br>', '')
                cleanr = re.compile('<.*?>')
                text = re.sub(cleanr, '', text)

            return text

        for lesson in timetable.get('roz_items').get('item'):
            d[lesson.get('date')] = {
                'day': get_day_name_by_date(lesson.get('date')),
                'date': lesson.get('date')[:5],
                'lessons': [],
            }

        for lesson in timetable.get('roz_items').get('item'):
            d[lesson.get('date')].get('lessons').append(
                clear_text(lesson.get('lesson_description'))
            )

        all_days_lessons = []

        for day in d.keys():
            all_days_lessons.append(d[day])

        if settings.USE_CACHE:
            request_key = '{}{}:{}-{}'.format(group, teacher, sdate, edate)
            _json = json.dumps(all_days_lessons, sort_keys=True, ensure_ascii=False, separators=(',', ':'), indent=2)
            core.Cache.put_in_cache(request_key, _json)

    except Exception as ex:

        core.log(msg='Помилка при відправленні запиту: {}\n.'.format(str(ex)), is_error=True)
        if settings.SEND_ERRORS_TO_ADMIN:
            message = f'\U000026A0\n**UserID:** {user_id}\n**Group:** {group} ({raw_group})\n\n{ex}'
            bot.send_message('204560928', message, reply_markup=keyboard, parse_mode='markdown')

        if settings.USE_CACHE:
            request_key = '{}{}:{}-{}'.format(group, teacher, sdate, edate)
            cached_timetable = core.Cache.get_from_cache(request_key)

            if cached_timetable:

                m = '\U000026A0 Встановити з\'єднання із сайтом Деканату не вдалося, тому показую розклад ' \
                    'станом на {} ' \
                    '(теоретично, його вже могли змінити)'.format(cached_timetable[0][2][11:])
                bot.send_message(user_id, m, reply_markup=keyboard)
                core.log(msg='Розклад видано з кешу')
                bot.send_message('204560928', 'Розклад видано з кешу', reply_markup=keyboard)
                return json.loads(cached_timetable[0][1])
            else:
                bot.send_message(user_id, '\U0001F680 На сайті ведуться технічні роботи. Спробуй пізніше', reply_markup=keyboard)
                return

        return []

    return all_days_lessons


def render_day_timetable(day_data, show_current=False, user_id=''):

    current_lesson = 0
    current_break = -1
    seconds_to_end = 0
    str_to_end = ''
    now_time = datetime.datetime.now().time()
    today = datetime.datetime.now()

    if show_current and settings.SHOW_TIME_TO_LESSON_END and \
            hasattr(settings, 'lessons_time') and hasattr(settings, 'breaks_time'):

        for i, lesson in enumerate(settings.lessons_time):
            if datetime.time(*lesson['start_time']) <= now_time <= datetime.time(*lesson['end_time']):
                current_lesson = i + 1
                time_to_end = today.replace(hour=lesson['end_time'][0], minute=lesson['end_time'][1]) - today
                seconds_to_end = time_to_end.total_seconds()
                break

        else:
            for i, _break in enumerate(settings.breaks_time):
                if datetime.time(*_break['start_time']) <= now_time <= datetime.time(*_break['end_time']):
                    current_break = i + 1
                    time_to_end = today.replace(hour=_break['end_time'][0], minute=_break['end_time'][1]) - today
                    seconds_to_end = time_to_end.total_seconds()
                    break

        str_to_end = core.datetime_to_string(seconds_to_end)

    if str(user_id) in ('204560928', '437220616',) or True:
        # Show random emoji for all users
        emoji = ('\U0001F41D', '\U0001F422', '\U0001F42C', '\U0001F43C', '\U0001F525',
                 '\U0001F537', '\U0001F608', '\U0001F697', '\U0001F346', '\U0001F340',
                 '\U0001F33A', '\U0001F388', '\U0001F365', '\U0001F33F',
                 '\U0001F648', '\U0001F649', '\U0001F6A9', '\U0001F937',
                 '\U000026F3', '\U00002B50', '\U0001F335', '\U0001F33B',
                 '\U0001F534', '\U0001F536', '\U0001F537', '\U0001F681')

        random_emoji_header = random.choice(emoji)
        day_timetable = '....:::: <b>{} {}</b> <i>{}</i> ::::....\n\n'.format(random_emoji_header,
                                                                              day_data['day'], day_data['date'])
    else:
        # TODO Delete it
        day_timetable = '....:::: <b>\U0001F4CB {}</b> <i>{}</i> ::::....\n\n'.format(day_data['day'], day_data['date'])

    lessons = day_data['lessons']

    start_index = 0
    end_index = len(lessons) - 1

    if not settings.SHOW_LESSONS_FROM_THE_FIRST:
        # Конструкція показує пари із першої існуючої
        for i in range(8):
            if lessons[i]:
                start_index = i
                break

    for i in range(end_index, -1, -1):
        if lessons[i]:
            end_index = i
            break

    timetable = core.get_str_timetable_list()

    for i in range(start_index, end_index + 1):
        if i == current_break:
            day_timetable += '\U000026F3 <b>Зараз перерва</b>  (<i>\U0001F55C {}</i>)\n\n'.format(str_to_end)

        if lessons[i]:
            lesson = lessons[i].replace('<span class="remote_work">дист.</span>', '(дистанційно \U0001F3E1)\n')
            if i + 1 == current_lesson:
                day_timetable += '<b>{} > {}</b> (<i>\U0001F55C {}</i>)\n<b>{}\n\n</b>'.format(emoji_numbers[i + 1],
                                                                                               timetable[i], str_to_end,
                                                                                               lesson)
            else:
                day_timetable += '{} > <b>{}</b> \n{}\n\n'.format(emoji_numbers[i + 1], timetable[i], lesson)
        else:
            if i + 1 == current_lesson:
                day_timetable += '<b>{} > {} </b>(<i>\U0001F55C {}</i>)\n<b>Вікно\U000026A1\n\n</b>'.format(emoji_numbers[i + 1],
                                                                                                            timetable[i], str_to_end)
            else:
                day_timetable += '{} > <b>{}</b>\nВікно \U000026A1\n\n'.format(emoji_numbers[i + 1], timetable[i])

    return day_timetable


@bot.message_handler(commands=['cu'])
def update_cache(message):

    user = core.User(message.chat)

    if len(message.text.split()) == 2:
        count = message.text.split()[1]
    else:
        count = 40

    if str(user.get_id()) not in settings.ADMINS_ID:
        bot.send_message(user.get_id(), 'Немає доступу :(')
        return

    bot.send_message(user.get_id(), 'Починаю оновлення розкладу.', reply_markup=keyboard, parse_mode='HTML')

    s = schedule_updater.update_cache(count)

    bot.send_message(user.get_id(), s, reply_markup=keyboard, parse_mode='HTML')


@bot.message_handler(commands=['ci'])
def cache_info(message):

    user = core.User(message.chat)

    cache_items_count = len(core.Cache.get_keys() or [])
    cache_requests = core.Cache.get_requests_to_cache()

    ans = 'В кеші <b>{}</b> записи(ів).\n' \
          'Кількість звернень: <b>{}</b>.\n'.format(cache_items_count, cache_requests[0][0])

    bot.send_message(user.get_id(), ans, reply_markup=keyboard, parse_mode='HTML')


@bot.message_handler(commands=['cc'])
def clear_cache(message):

    user = core.User(message.chat)

    if str(user.get_id()) not in settings.ADMINS_ID:
        bot.send_message(user.get_id(), 'Немає доступу :(')
        return

    core.Cache.clear_cache()

    bot.send_message(user.get_id(), 'Кеш був очищений.')


@bot.message_handler(commands=['get_log_files'])
def get_log_file(message):

    user = core.User(message.chat)

    if str(user.get_id()) not in settings.ADMINS_ID:
        bot.send_message(user.get_id(), 'Немає доступу :(')
        return

    with open(os.path.join(settings.BASE_DIR, 'bot_log.log'), 'r', encoding="utf-8") as log_file:
        bot.send_document(user.get_id(), log_file)

    with open(os.path.join(settings.BASE_DIR, 'error_log.txt'), 'r', encoding="utf-8") as error_log_file:
        bot.send_document(user.get_id(), error_log_file)


@bot.message_handler(commands=['get_db_file'])
def get_db_file(message):
    user = core.User(message.chat)

    if str(user.get_id()) not in settings.ADMINS_ID:
        bot.send_message(user.get_id(), 'Немає доступу :(')
        return

    with open(os.path.join(settings.BASE_DIR, settings.DATABASE), 'rb') as db_file:
        bot.send_document(user.get_id(), db_file)


@bot.message_handler(commands=['log'])
def get_logs(message):

    user = core.User(message.chat)

    if str(user.get_id()) not in settings.ADMINS_ID:
        bot.send_message(user.get_id(), 'Немає доступу :(')
        return

    if len(message.text.split()) == 2:
        count = int(message.text.split()[1])
    else:
        count = 65

    with open(os.path.join(settings.BASE_DIR, 'bot_log.log'), 'r', encoding="utf-8") as log_file:
        log_lines = log_file.readlines()

    logs = ''

    for line in log_lines[-count:]:
        logs += line

    bot.send_message(user.get_id(), logs, reply_markup=keyboard)


@bot.message_handler(commands=['elog'])
def get_error_logs(message):

    user = core.User(message.chat)

    if str(user.get_id()) not in settings.ADMINS_ID:
        bot.send_message(user.get_id(), 'Немає доступу :(')
        return

    if len(message.text.split()) == 2:
        count = int(message.text.split()[1])
    else:
        count = 65

    with open(os.path.join(settings.BASE_DIR, 'error_log.txt'), 'r', encoding="utf-8") as log_file:
        log_lines = log_file.readlines()

    logs = ''

    for line in log_lines[-count:]:
        logs += line

    bot.send_message(user.get_id(), logs, reply_markup=keyboard)


@bot.message_handler(commands=['get_webhook_info'])
def bot_admin_get_webhook_info(message):

    user = core.User(message.chat)

    if str(user.get_id()) not in settings.ADMINS_ID:
        bot.send_message(user.get_id(), 'Немає доступу :(')
        return

    webhook = bot.get_webhook_info()

    msg = f"*URL:* {webhook.url or '-'}\n*Счікує обробки:* {webhook.pending_update_count}\n" \
          f"*Остання помилка:* {webhook.last_error_message} ({webhook.last_error_date})"

    bot.send_message(user.get_id(), msg, reply_markup=keyboard, parse_mode='markdown')


@bot.message_handler(commands=['vip'])
def set_vip_by_id(message):

    user = core.User(message.chat)

    if str(user.get_id()) not in settings.ADMINS_ID:
        bot.send_message(user.get_id(), 'Немає доступу :(')
        return

    if len(message.text.split()) == 3:
        user_id = message.text.split()[1]

        if message.text.split()[2] == '+':
            core.AdService.set_vip_by_id(user_id, 1)
            bot.send_message(user.get_id(), 'VIP статус установлено.', reply_markup=keyboard)
        elif message.text.split()[2] == '-':
            core.AdService.set_vip_by_id(user_id, 0)
            bot.send_message(user.get_id(), 'VIP статус видалено.', reply_markup=keyboard)
    else:

        bot.send_message(user.get_id(), 'Неправильний формат. Треба /vip <id> <+, ->', reply_markup=keyboard)


@bot.message_handler(commands=['ahelp'])
def bot_admin_help_cmd(message):

    cmds = '/ci - інформація про кеш\n' \
           '/cu [N] - оновити кеш для N груп (по зам. 40)\n' \
           '/cc - очистити кеш\n' \
           '/log [N] - показати N рядків логів (по зам. 65)\n' \
           '/elog [N] - показати N рядків логів із помилками (по зам. 65)\n' \
           '/get_log_files - завантажити файли із логами(запити і помилки)\n' \
           '/get_webhook_info - інформація про Веб-хук\n' \
           '/get_db_file - заантажити файл із БД\n' \
           '/vip <user_id> <+/-> дати/забрати ВІП статус оголошень\n' \
           '/da <user_id> - видалити оголошення'

    bot.send_message(message.chat.id, cmds, reply_markup=keyboard)


@bot.message_handler(commands=['da'])
def del_ad_by_id(message):

    user = core.User(message.chat)

    if str(user.get_id()) not in settings.ADMINS_ID:
        bot.send_message(user.get_id(), 'Немає доступу :(')
        return

    if len(message.text.split()) == 2:
        user_id = message.text.split()[1]
        core.AdService.delete_user_ad(user_id)
        bot.send_message(user.get_id(), 'Оголошення видалено.', reply_markup=keyboard)
    else:

        bot.send_message(user.get_id(), 'Неправильний формат. Треба /da <user_id>', reply_markup=keyboard)


@bot.message_handler(commands=['start'])
def start_handler(message):

    user = core.User(message.chat)

    if user.get_group():
        msg = 'Ти вже зареєстрований(на), твоя група - {}'.format(user.get_group())
        bot.send_message(chat_id=user.get_id(), text=msg, parse_mode='HTML', reply_markup=keyboard)
        return

    if user.get_id() < 0:
        msg = 'Сорі, братан, мене у групу нізя додавати) Якщо тут якась помилка, напиши сюди - @koocherov'
        bot.send_message(chat_id=user.get_id(), text=msg, parse_mode='HTML')
        return

    start_text_file = open(os.path.join(settings.BASE_DIR, 'data', 'start.txt'), 'r', encoding="utf-8")

    msg = 'Хай, {} 😊. {}' \
          '<b>Змінити ти її зможеш в пункті меню {}</b>'.format(message.chat.first_name,
                                                                start_text_file.read(),
                                                                KEYBOARD['HELP'])
    start_text_file.close()

    sent = bot.send_message(chat_id=user.get_id(), text=msg, parse_mode='HTML')
    bot.register_next_step_handler(sent, set_group)


@bot.message_handler(commands=['stats'])
def stats_handler(message):
    #TODO complete it or delete
    user = core.User(message.chat)
    users_count_from_group = user.get_users_count_from_group()
    requests_count = user.get_user_requests_count()

    msg = '<b>\U0001F47D Твоя статистика:</b>\n\n' \
          '<i>Кількість твоїх запитів:</i> {}\n' \
          '<i>Людей із твоєї групи:</i> {}\n\n' \
          '<b>\U0001f916 Статистика бота:</b>\n\n'.format(requests_count, users_count_from_group)

    bot.send_message(chat_id=message.chat.id, text=msg, parse_mode='HTML', reply_markup=keyboard)


def send_or_split_by_separate_messages(timetable_days, user, additional_message=''):

    rendered_timetable = additional_message

    for day in timetable_days:
        rendered_timetable += render_day_timetable(day, user_id=user.get_id())

    if len(rendered_timetable) < 4090:
        bot.send_message(user.get_id(), rendered_timetable, parse_mode='HTML', reply_markup=keyboard)
        return

    rendered_timetable_for_day = additional_message

    while timetable_days:

        timetable_day = timetable_days.pop(0)
        rendered_timetable_for_day += render_day_timetable(timetable_day, user_id=user.get_id())

        if len(rendered_timetable_for_day) > 4090:
            rendered_timetable_for_day = rendered_timetable_for_day[:4040] + '\n\n\U000026A0<b> ' \
                                                                             'Весь текст не помістився ' \
                                                                             'у повідомлені \U000026A0</b>'

        bot.send_message(user.get_id(), rendered_timetable_for_day, parse_mode='HTML', reply_markup=keyboard)
        rendered_timetable_for_day = ''


@bot.callback_query_handler(func=lambda call_back: call_back.data in ('\U00002B07 Поточний', '\U000027A1 Наступний'))
def week_schedule_handler(call_back):

    user = core.User(call_back.message.chat)
    user_group = user.get_group()
    req = call_back.data

    today = datetime.date.today()
    current_week_day_number = today.isoweekday()
    diff_between_saturday_and_today = 6 - current_week_day_number
    last_week_day = today + datetime.timedelta(days=diff_between_saturday_and_today)

    next_week_first_day = today + datetime.timedelta(days=diff_between_saturday_and_today + 2)
    next_week_last_day = today + datetime.timedelta(days=diff_between_saturday_and_today + 7)

    timetable_data = list()
    timetable_for_week_empty = str()

    if req == '\U00002B07 Поточний':
        timetable_for_week_empty = 'На цей тиждень пар не знайдено.'
        timetable_data = get_timetable(group=user_group, sdate=today.strftime('%d.%m.%Y'),
                                       edate=last_week_day.strftime('%d.%m.%Y'), user_id=user.get_id())
    if req == '\U000027A1 Наступний':
        timetable_for_week_empty = 'На наступний тиждень пар не знайдено.'
        timetable_data = get_timetable(group=user_group, sdate=next_week_first_day.strftime('%d.%m.%Y'),
                                       edate=next_week_last_day.strftime('%d.%m.%Y'), user_id=user.get_id())

    try:
        bot.delete_message(chat_id=user.get_id(), message_id=call_back.message.message_id)
    except ApiTelegramException:
        pass

    if timetable_data:
        send_or_split_by_separate_messages(timetable_data, user)

    elif isinstance(timetable_data, list) and not len(timetable_data):
        bot_send_message_and_post_check_group(user.get_id(), timetable_for_week_empty, user_group)


@bot.callback_query_handler(func=lambda call_back: call_back.data.startswith('SET_GP'))
def update_group_handler(call_back):

    user = core.User(call_back.message.chat)
    request = call_back.data

    try:
        bot.delete_message(chat_id=user.get_id(), message_id=call_back.message.message_id)
    except ApiTelegramException:
        pass

    _, group = request.split(':')

    if group == 'INPUT':

        sent = bot.send_message(user.get_id(), 'Введи назву групи')
        bot.register_next_step_handler(sent, set_group)

    else:
        call_back.message.text = group
        set_group(call_back.message)


@bot.callback_query_handler(func=lambda call_back: call_back.data in (KEYBOARD['MAIN_MENU'], KEYBOARD['CHANGE_GROUP']))
def help_menu_handler(call_back):

    user = core.User(call_back.message.chat)
    request = call_back.data

    try:
        bot.delete_message(chat_id=user.get_id(), message_id=call_back.message.message_id)
    except ApiTelegramException:
        pass

    if request == KEYBOARD['CHANGE_GROUP']:

        msg = f'Твоя поточна група: <b>{user.get_group()}</b>\nвведи нову назву:'

        if not core.is_group_valid(user.get_group()):
            possible_groups = core.get_possible_groups(user.get_group())

            if possible_groups:

                msg = "Твоя поточна група: <b>{}</b>\n\n" \
                      "Вибери іншу із списку, або натисни\n {}:".format(user.get_group(), KEYBOARD['INPUT_GROUP'])

                possible_groups_kb = telebot.types.InlineKeyboardMarkup()
                for group in possible_groups:
                    possible_groups_kb.row(
                        telebot.types.InlineKeyboardButton(group, callback_data=f'SET_GP:{group}')
                    )
                possible_groups_kb.row(
                    telebot.types.InlineKeyboardButton(KEYBOARD['INPUT_GROUP'], callback_data=f'SET_GP:INPUT')
                )
                possible_groups_kb.row(
                    telebot.types.InlineKeyboardButton(KEYBOARD['MAIN_MENU'], callback_data=KEYBOARD['MAIN_MENU'])
                )

                bot.send_message(user.get_id(), msg, parse_mode='HTML', reply_markup=possible_groups_kb)
                return

        sent = bot.send_message(user.get_id(), msg, parse_mode='HTML')
        bot.register_next_step_handler(sent, set_group)
        return

    bot.send_message(user.get_id(), 'Меню так меню', reply_markup=keyboard, parse_mode='HTML')


def bot_send_message_and_post_check_group(chat_id='', text='', user_group='', parse_mode='HTML'):

    bot.send_message(chat_id=chat_id, text=text, parse_mode=parse_mode, reply_markup=keyboard)

    if not core.is_group_valid(user_group):
        possible_groups = core.get_possible_groups(user_group)
        text = '\n\nТвоєї групи <b>{}</b> немає в базі розкладу, ' \
            'тому перевір правильність вводу.'.format(user_group)

        if possible_groups:
            text += '\n\n<b>Можливі варіанти:</b>\n' + '\n'.join(possible_groups)


        change_group_kb = telebot.types.InlineKeyboardMarkup()
        change_group_kb.row(
            telebot.types.InlineKeyboardButton(KEYBOARD['CHANGE_GROUP'], callback_data=KEYBOARD['CHANGE_GROUP'])
        )

        bot.send_message(chat_id=chat_id, text=text, parse_mode=parse_mode, reply_markup=change_group_kb)


def set_group(message):

    user = core.User(message.chat)
    group = core.delete_html_tags(message.text)

    if group == '/start':
        sent = bot.send_message(message.chat.id, 'Вкажи свою групу')
        bot.register_next_step_handler(sent, set_group)
        return

    if group in list(KEYBOARD.values()):
        msg = 'Введи назву групи'
        sent = bot.send_message(message.chat.id, msg, parse_mode='HTML')
        bot.register_next_step_handler(sent, set_group)
        return

    if group == 'Відміна':
        current_user_group = user.get_group()
        bot.send_message(message.chat.id, 'Добре, залишимо групу {}.'.format(current_user_group), reply_markup=keyboard)
        return

    if not core.is_group_valid(group):

        possible_groups = core.get_possible_groups(group)
        msg = 'Групу <b>{}</b> я зберіг, але її немає в базі розкладу. ' \
              'Тому якщо розклад не буде відображатись - перевір правильність вводу'.format(group)

        if possible_groups:
            msg += '\n\n<b>Можливі варіанти:</b>\n' + '\n'.join(possible_groups)

        msg += '\n\n\U0001f9d0 Щоб змінити групу жми: {} > {}'.format(KEYBOARD['HELP'], KEYBOARD['CHANGE_GROUP'])

    else:
        msg = '\U0001f917 Добро, буду показувати розклад для групи <b>{}</b>.'.format(group)

    user.update_group(group) if user.get_group() else user.registration(group)

    bot.send_message(message.chat.id, msg, reply_markup=keyboard, parse_mode='HTML')


@bot.callback_query_handler(func=lambda call_back: call_back.data[:2] in ('_S', '_Z', '_W'))
def schedule_teacher_time_handler(call_back):

    user = core.User(call_back.message.chat)
    time_type, teacher_name = call_back.data.split(':')
    teacher_name = core.get_teacher_fullname_by_first_symbols(teacher_name)

    try:
        bot.delete_message(chat_id=user.get_id(), message_id=call_back.message.message_id)
    except ApiTelegramException:
        pass

    if time_type == '_S':  # Today

        today = datetime.date.today().strftime('%d.%m.%Y')
        timetable_data = get_timetable(teacher=teacher_name, user_id=user.get_id(), sdate=today, edate=today)

        if timetable_data:
            timetable_for_today = '\U0001F464 Пари для <b>{}</b> на сьогодні:\n\n'.format(teacher_name)
            timetable_for_today += render_day_timetable(timetable_data[0], show_current=True, user_id=user.get_id())
        elif isinstance(timetable_data, list) and not len(timetable_data):
            timetable_for_today = 'На сьогодні пар у <b>{}</b> не знайдено.'.format(teacher_name)
        else:
            return
        bot.send_message(user.get_id(), timetable_for_today, reply_markup=keyboard, parse_mode='HTML')

    if time_type == '_Z':  # Tomorrow

        tomorrow = datetime.date.today() + datetime.timedelta(days=1)
        tom_day = tomorrow.strftime('%d.%m.%Y')

        timetable_data = get_timetable(teacher=teacher_name, sdate=tom_day, edate=tom_day, user_id=user.get_id())

        if timetable_data:
            timetable_for_a_week = '\U0001F464 Пари для <b>{}</b> на завтра:\n\n'.format(teacher_name)
            timetable_for_a_week += render_day_timetable(timetable_data[0], user_id=user.get_id())

        elif isinstance(timetable_data, list) and not len(timetable_data):
            timetable_for_a_week = 'На завтра пар для викладача <b>{}</b> не знайдено.'.format(teacher_name)
        else:
            return

        bot.send_message(user.get_id(), timetable_for_a_week, parse_mode='HTML', reply_markup=keyboard)

    if time_type == '_W':  # Week

        in_week = datetime.date.today() + datetime.timedelta(days=7)

        in_week_day = in_week.strftime('%d.%m.%Y')
        today = datetime.date.today().strftime('%d.%m.%Y')

        timetable_data = get_timetable(teacher=teacher_name, sdate=today, edate=in_week_day, user_id=user.get_id())

        if timetable_data:
            timetable_for_a_week = f'\U0001F464 Розклад на тиждень у <b>{teacher_name}</b>:\n\n'
            send_or_split_by_separate_messages(timetable_data, user, timetable_for_a_week)
        elif isinstance(timetable_data, list) and not len(timetable_data):
            timetable_for_a_week = '\U0001f914 На тиждень пар у викладача <b>{}</b> не знайдено.'.format(teacher_name)
            bot.send_message(user.get_id(), timetable_for_a_week, reply_markup=keyboard, parse_mode='HTML')


@bot.callback_query_handler(func=lambda call_back: call_back.data == 'Ввести прізвище'
                                                   or core.is_teacher_valid(core.get_teacher_fullname_by_first_symbols(call_back.data)))
def last_teacher_handler(call_back):

    user = core.User(call_back.message.chat)

    try:
        bot.delete_message(chat_id=user.get_id(), message_id=call_back.message.message_id)
    except ApiTelegramException:
        pass

    if call_back.data == 'Ввести прізвище':

        sent = bot.send_message(call_back.message.chat.id, 'Введи прізвище')
        bot.register_next_step_handler(sent, select_teacher_by_second_name)

    else:
        user = core.User(call_back.message.chat)
        teacher_full_name = call_back.data

        select_time_to_show_teachers_schedule(user.get_id(), teacher_full_name)


def select_time_to_show_teachers_schedule(chat_id, teacher_name):

    teacher_name = core.get_teacher_fullname_by_first_symbols(teacher_name)
    core.Teachers.add_teacher_to_user(chat_id, teacher_name)

    select_time_to_show_keyboard = telebot.types.InlineKeyboardMarkup()
    select_time_to_show_keyboard.row(
        telebot.types.InlineKeyboardButton(KEYBOARD['TODAY'], callback_data='{}:{}'.format('_S', teacher_name[:28])),
        telebot.types.InlineKeyboardButton(KEYBOARD['TOMORROW'], callback_data='{}:{}'.format('_Z', teacher_name[:28]))
    )
    select_time_to_show_keyboard.row(
        telebot.types.InlineKeyboardButton(KEYBOARD['FOR_A_WEEK'], callback_data='{}:{}'.format('_W', teacher_name[:28])),
    )

    msg = 'На коли показати розклад для <b>{}</b>?'.format(teacher_name)

    bot.send_message(chat_id, msg, reply_markup=select_time_to_show_keyboard, parse_mode='HTML')


def select_teacher_by_second_name(message):

    requested_teacher_lastname = message.text.upper().split()[0]
    user = core.User(message.chat)

    core.log(message.chat, '> (по викладачу) {}'.format(requested_teacher_lastname.capitalize()))
    possible_teaches = []

    try:
        with open(os.path.join(settings.BASE_DIR, 'data', 'teachers.json'), 'r', encoding="utf-8") as file:
            all_teachers = json.loads(file.read())
    except Exception as ex:
        bot.send_message(message.chat.id, 'Даний функціонал тимчасово не працює.', reply_markup=keyboard)
        core.log(msg='Помилка із файлом викладачів: {}\n'.format(str(ex)), is_error=True)
        return

    possible_teacher = core.get_possible_teacher_by_lastname(requested_teacher_lastname)

    for teacher in all_teachers:
        if teacher.split()[0].upper() == possible_teacher:
            possible_teaches.append(teacher)

    if len(possible_teaches) == 1:
        select_time_to_show_teachers_schedule(user.get_id(), possible_teaches[0])

    elif len(possible_teaches) > 1:
        teachers_keyboard = telebot.types.InlineKeyboardMarkup()
        for teacher in possible_teaches:
            teachers_keyboard.row(
                telebot.types.InlineKeyboardButton('\U0001F464 ' + teacher, callback_data=teacher[:30]),
            )

        bot.send_message(user.get_id(), 'Вибери викладача:', reply_markup=teachers_keyboard)

    else:
        msg = 'Не можу знайти викладача з прізвищем <b>{}</b>. Якщо при вводі була допущена помилка ' \
              '- знову натисни в меню кнопку "{}" і введи заново.'.format(requested_teacher_lastname.capitalize(),
                                                                          KEYBOARD['FOR_A_TEACHER'])

        bot.send_message(user.get_id(), msg, reply_markup=keyboard, parse_mode='HTML')


def show_other_group(message):

    group = message.text
    user = core.User(message.chat)
    core.log(message.chat, '> (по групі) {}'.format(group))
    bot.send_chat_action(message.chat.id, "typing")

    if group == KEYBOARD['MAIN_MENU']:
        bot.send_message(message.chat.id, 'Окей', reply_markup=keyboard, parse_mode='HTML')
        return

    if not core.is_group_valid(group):

        possible_groups = core.get_possible_groups(group)
        msg = '\U0001f914 Групи <b>{}</b> немає в базі розкладу.\n\n'.format(group)

        if possible_groups:
            msg += '<b>Можливі варіанти:</b>\n'
            groups_kb = telebot.types.ReplyKeyboardMarkup(resize_keyboard=True, one_time_keyboard=True)
            groups_kb.row(KEYBOARD['MAIN_MENU'])

            for group in possible_groups:
                msg += '{}\n'.format(group)
                groups_kb.row(group)

            sent = bot.send_message(message.chat.id, msg, parse_mode='HTML', reply_markup=groups_kb)
            bot.register_next_step_handler(sent, show_other_group)
            return

        bot.send_message(message.chat.id, msg, reply_markup=keyboard, parse_mode='HTML')
        return

    in_week = datetime.date.today() + datetime.timedelta(days=7)
    in_week_day = in_week.strftime('%d.%m.%Y')
    today = datetime.date.today().strftime('%d.%m.%Y')

    timetable_data = get_timetable(group=group, sdate=today, edate=in_week_day, user_id=message.chat.id)
    info = f'\U00002139 <b>Розклад на тиждень для групи {message.text}:</b>\n\n'

    if timetable_data:
        send_or_split_by_separate_messages(timetable_data, user, info)
    elif isinstance(timetable_data, list) and not len(timetable_data):
        timetable_for_week = 'На тиждень пар для групи {} не знайдено.'.format(group)
        bot.send_message(message.chat.id, timetable_for_week, parse_mode='HTML', reply_markup=keyboard)


def add_ad(message):

    user_id = message.chat.id
    text = message.text
    username = message.chat.username

    if text == KEYBOARD['MAIN_MENU']:
        msg = 'Окей'
        bot.send_message(message.chat.id, msg, parse_mode='HTML', reply_markup=keyboard)
        return

    if text in [KEYBOARD['AD_ADD'], KEYBOARD['AD_LIST']]:
        msg = 'Помилка. Введи ще раз.'
        sent = bot.send_message(message.chat.id, msg, parse_mode='HTML')
        bot.register_next_step_handler(sent, add_ad)
        return

    if not core.AdService.add_advertisement(user_id, username, text):
        bot.send_message(user_id, 'Помилка.', reply_markup=keyboard, parse_mode='HTML')
        return

    msg = core.AdService.render_ads()
    bot.send_message('204560928', '\U00002139 <b>@{}</b> >  {}'.format(username, text), reply_markup=keyboard, parse_mode='HTML')
    bot.send_message(user_id, '\U00002705 Оголошення додано!')
    bot.send_message(user_id, msg, reply_markup=keyboard, parse_mode='HTML')


def process_menu(message):

    if message.text == KEYBOARD['AD_ADD']:

        if not message.chat.username:
            bot.send_message(message.chat.id, 'Щоб додавати оголошення постав логін. '
                                              'Зробити це можна в налаштуваннях Телеграму.', reply_markup=keyboard)
            return

        if core.AdService.check_if_user_have_ad(message.chat.id):
            bot.send_message(message.chat.id, 'Одночасно можна додавати тільки одне оголошення. '
                                              'Видаліть попереднє.', reply_markup=keyboard)
            return

        sent = bot.send_message(message.chat.id, 'Введи текст оголошення (до 120 символів)', parse_mode='HTML')
        bot.register_next_step_handler(sent, add_ad)

    elif message.text == KEYBOARD['MAIN_MENU']:
        bot.send_message(message.chat.id, 'По рукам.', parse_mode='HTML', reply_markup=keyboard)

    elif message.text == KEYBOARD['AD_DEL']:
        core.AdService.delete_user_ad(message.chat.id)
        bot.send_message(message.chat.id, '\U00002705 Твоє оголошення видалено!', parse_mode='HTML', reply_markup=keyboard)

    else:
        bot.send_message(message.chat.id, 'Не розумію :(', parse_mode='HTML', reply_markup=keyboard)


@app.route('/fl/login/', methods=['POST', 'GET'])
def admin_login():

    if not settings.BOT_TOKEN:
        return 'Set bot token in settings.py'

    if session.get('login'):
        return redirect(url_for('admin_metrics'))

    if request.method == 'GET':
        return render_template('login.html')

    req_ip = request.environ.get('HTTP_X_REAL_IP', request.remote_addr)
    req_agent = request.user_agent

    if request.method == 'POST' and request.form.get('password') == settings.ADMIN_PASSWORD:
        session['login'] = True
        msg = f'Авторизація в панелі адміністратора.\n<b>IP: </b>{req_ip}\n<b>UA: </b>{req_agent}'
        bot.send_message('204560928', msg, parse_mode='HTML')
        return redirect(url_for('admin_metrics'))

    else:

        msg = f'Неправильний пароль під час авторизації в панелі адміністратора.\n' \
              f'<b>IP: </b>{req_ip}\n<b>UA: </b>{req_agent}'

        bot.send_message('204560928', msg, parse_mode='HTML')

        return 'Неправильний пароль'


@app.route('/fl/logout/')
def admin_logout():

    if session.get('login'):
        session['login'] = False
    return admin_login()


@app.route('/fl/debug/')
def admin_debug():

    import os

    return str(os.environ)


@app.route('/fl/metrics/')
def admin_metrics():

    if not session.get('login'):
        return admin_login()

    all_users_count = core.MetricsManager.get_all_users_count()
    all_groups_count = core.MetricsManager.get_all_groups_count()
    users_registered_week = core.MetricsManager.get_number_of_users_registered_during_the_week()
    active_today_users_count = core.MetricsManager.get_active_today_users_count()
    active_yesterday_users_count = core.MetricsManager.get_active_yesterday_users_count()
    active_week_users_count = core.MetricsManager.get_active_week_users_count()
    top_groups_by_users = core.MetricsManager.get_top_groups(15)
    top_groups_by_requests = core.MetricsManager.get_top_request_groups_during_the_week(15) or []
    top_teachers = core.Teachers.get_top_teachers(15)
    saved_teachers_count = core.Teachers.get_users_saved_teachers_count()

    try:
        forecast_update_date = os.path.getmtime(os.path.join(settings.BASE_DIR, 'data', 'groups.json'))
        groups_update_time = datetime.datetime.fromtimestamp(forecast_update_date).strftime('%d.%m.%Y %H:%M')
    except Exception:
        groups_update_time = '-'

    try:
        forecast_update_date = os.path.getmtime(os.path.join(settings.BASE_DIR, 'data', 'teachers.json'))
        teachers_update_time = datetime.datetime.fromtimestamp(forecast_update_date).strftime('%d.%m.%Y %H:%M')
    except Exception:
        teachers_update_time = '-'

    metrics_values = {
        'all_users_count': all_users_count,
        'all_groups_count': all_groups_count,
        'users_registered_week': users_registered_week,
        'active_today_users_count': active_today_users_count,
        'active_yesterday_users_count': active_yesterday_users_count,
        'active_week_users_count': active_week_users_count,
        'top_groups_by_users': top_groups_by_users,
        'top_groups_by_requests': top_groups_by_requests,
        'top_teachers': top_teachers,
        'groups_update_time': groups_update_time,
        'teachers_update_time': teachers_update_time,
        'saved_teachers_count': saved_teachers_count,
        'webhook': bot.get_webhook_info(),
        'timetable_url': settings.TIMETABLE_URL,
        'api_url': settings.API_LINK,
    }

    return render_template('metrics.html', data=metrics_values)


@app.route('/fl/del_user/<user_id>')
def admin_del_user(user_id):

    if not session.get('login'):
        return admin_login()

    data = {}

    u = core.User.get_userinfo_by_id(user_id)
    core.User.delete_user(user_id)

    if u:
        data['message'] = 'Користувач <b>{} {}</b> був успішно видалений. <br> ' \
                          '<b>група:</b> {}, <b>реєстрація:</b> {}, ' \
                          '<b>остання активність:</b> {}'.format(u[2], u[3] or '', u[4], u[6], u[7])
    else:
        data['message'] = 'Такого користувача не знайдено.'

    users = core.User.get_users()
    data['users'] = users

    return render_template('users.html', data=data)


@app.route('/fl/users/')
def admin_users():

    if not session.get('login'):
        return admin_login()

    data = {
        'users': core.User.get_users()
    }

    return render_template('users.html', data=data)


@app.route('/')
def admin_redirect_to_login():

    return admin_login()


@app.route('/fl/send_message', methods=['POST', 'GET'])
def admin_send_message():

    if not session.get('login'):
        return admin_login()

    telegram_id = request.form.get('usr-id')
    text = str(request.form.get('text')).strip()

    data = {
        'chat_id': telegram_id,
        'parse_mode': 'HTML',
        'text': '\U0001f916 <b>Бот</b>:\n\n' + text
    }

    r = requests.get('https://api.telegram.org/bot{}/sendMessage'.format(settings.BOT_TOKEN), params=data).json()

    if r.get('ok'):
        data['message'] = 'Відправлено: <b>{}</b>'.format(text)
    else:
        data['message'] = 'Помилка {}: {}'.format(r.get('error_code'), r.get('description'))

    return admin_user_statistics(telegram_id, data['message'])


@app.route('/fl/statistics_by_types_during_the_week')
def statistics_by_types_during_the_week():

    stats = core.MetricsManager.get_statistics_by_types_during_the_week()

    return jsonify(data=stats)


@app.route('/fl/last_days_statistics')
def last_days_statistics():

    days_statistics = core.MetricsManager.get_last_days_statistics()

    stats = {'labels': [], 'data': []}

    def sort_by_date(input_str):
        return datetime.datetime.strptime(input_str + '.' + str(datetime.date.today().year), '%d.%m.%Y')

    # Sorting by dates
    for day_stat in sorted(days_statistics, key=sort_by_date):

        stats['labels'].append(day_stat)
        stats['data'].append(days_statistics[day_stat])

    return jsonify(data=stats)


@app.route('/fl/admin_last_requests')
def admin_last_requests():

    offset = request.args.get('offset')

    last_requests = core.MetricsManager.get_last_requests(offset)

    return jsonify(last_requests)


@app.route('/fl/last_hours_statistics')
def last_hours_statistics():

    today_hours_statistics = core.MetricsManager.get_hours_statistics()
    yesterday_hours_statistics = core.MetricsManager.get_hours_statistics(day_delta=1)
    two_days_ago_statistics = core.MetricsManager.get_hours_statistics(day_delta=2)

    stats = {'labels': [], 'stats_data': {'today': [], 'yesterday': [], 'two_days_ago': []}}

    def sort_by_date(input_str):
        return datetime.datetime.strptime(input_str, '%Y-%m-%d %H:%M')

    [stats['labels'].append('{}:00'.format(hour) ) for hour in range(24)]

    for day_stat in sorted(today_hours_statistics, key=sort_by_date):
        stats['stats_data']['today'].append(today_hours_statistics[day_stat])

    for day_stat in sorted(yesterday_hours_statistics, key=sort_by_date):
        stats['stats_data']['yesterday'].append(yesterday_hours_statistics[day_stat])

    for day_stat in sorted(two_days_ago_statistics, key=sort_by_date):
        stats['stats_data']['two_days_ago'].append(two_days_ago_statistics[day_stat])

    return jsonify(data=stats)


@app.route('/fl/update_groups')
def admin_update_groups():

    if not session.get('login'):
        return admin_login()

    try:
        updated = core.update_all_groups()

        if updated:
            msg = 'Список груп оновлено. Завантажено {} груп.<br>'.format(len(updated))
            msg += str(updated)
            return msg
    except Exception as ex:
        return f'Помилка<br><br>{str(ex)}'
    return 'Помилка при оновленні'


@app.route('/fl/update_teachers')
def admin_update_teachers():

    if not session.get('login'):
        return admin_login()

    updated = core.update_all_teachers()

    if updated:
        msg = 'Список викладачів оновлено. Завантажено {} імен.<br>'.format(len(updated))
        msg += str(updated)
        return msg
    return 'Помилка при оновленні'


@app.route('/fl/user/<user_id>')
def admin_user_statistics(user_id, msg=''):

    if not session.get('login'):
        return admin_login()

    data = {
        'user': core.User.get_userinfo_by_id(user_id),
        'actions': core.MetricsManager.get_stats_by_user_id(user_id),
        'saved_teachers': core.Teachers.get_user_saved_teachers(user_id),
        'message': msg
    }

    return render_template('user_stat.html', data=data)


@app.route('/fl/settings', methods=['POST', 'GET'])
def admin_settings():

    # TODO done it or delete

    if not session.get('login'):
        return admin_login()

    if request.method == 'GET':
        return render_template('settings.html')

    timetable = request.form.get('set_timetable')

    lessons_full_time = timetable.split(';')  # Split to the single rows
    lessons_full_time = map(lambda s: s.strip(), lessons_full_time)  # Delete \n\r in the string

    lessons_time_generated = []

    for lesson_time in lessons_full_time:
        lesson_start_time = lesson_time.split('-')[0]
        lesson_end_time = lesson_time.split('-')[1]
        lessons_time_generated.append(
            {
                'start_time': [lesson_start_time.split(':')[0], lesson_start_time.split(':')[1]],  # 0 hour, 1 - minutes
                'end_time': [lesson_end_time.split(':')[0], lesson_end_time.split(':')[1]],
            }
        )

    return render_template('settings.html')


@app.route('/fl/upd_cache_cron')
def admin_update_cache():

    bot.send_message('204560928', 'Починаю оновлення розкладу через крон.', reply_markup=keyboard, parse_mode='HTML')
    core.log(msg='Оновлення розкладу через крон')

    msg = schedule_updater.update_cache(60)
    updated_groups = core.update_all_groups()
    updated_teachers = core.update_all_teachers()

    if updated_groups:
        msg += '\n\nСписок груп оновлено - {}.'.format(len(updated_groups))

    if updated_teachers:
        msg += '\nСписок викладачів - {}.'.format(len(updated_teachers))

    bot.send_message('204560928', msg, reply_markup=keyboard, parse_mode='HTML')

    core.log(msg='Розклад по крону оновлено.')
    msg = '<!doctype html>\n<head><meta charset="utf-8"><head>\n<body>' + msg + '</body></html>'

    return msg


@app.route('/fl/init/')
def admin_init():

    if not session.get('login'):
        return admin_login()

    if request.args.get('hook_id') == '0':
        bot.delete_webhook()
        return 'Webhook deleted'

    core.DBManager.create_db_tables()

    domain = settings.WEBHOOK_DOMAINS.get(request.args.get('hook_id'), request.host_url)

    try:
        status = bot.set_webhook(f'{domain}/fl/{settings.WEBHOOK_PATH}', max_connections=2)
    except telebot.apihelper.ApiException as ex:

        status = False
        error_text = ex.result.json().get('description')

    if status:
        bot.send_message('204560928', f'Запуск через /fl/init\nВебхук: {bot.get_webhook_info().url}')
        core.log(msg='Запуск через url. Веб-хук встановлено: {}.'.format(bot.get_webhook_info().url))

        url = bot.get_webhook_info().url
        url = url.replace('<', '&lt;')
        url = url.replace('>', '&gt;')

        return f'Успіх<br><br> Встановлено вебхук на: {url}'
    else:
        return f'{domain}<br><br>Помилка<br><br>{error_text}'


@app.route(f'/fl/{settings.WEBHOOK_PATH}', methods=['POST'])
def webhook():

    json_string = request.get_data().decode('utf-8')
    update = telebot.types.Update.de_json(json_string)
    bot.process_new_updates([update])

    return "ok", 200


@app.route('/fl/git_pull', methods=['POST', 'GET'])
def git_pull_handler():

    if not session.get('login'):
        return admin_login()

    import git

    g = git.cmd.Git(settings.BASE_DIR)
    result = g.pull()

    return result


@bot.message_handler(content_types=["text"])
def main_menu(message):

    bot.send_chat_action(message.chat.id, "typing")

    user = core.User(message.chat)
    user_group = user.get_group()
    request = message.text

    if not user_group:
        bot.send_message(user.get_id(), 'Не знайшов твою групу. Введи /start, і вкажи її.')
        return

    def is_date_request_or_other():

        regs = (r'^(\d{1,2})\.(\d{1,2})$',
                r'^(\d{1,2})\.(\d{1,2})-(\d{1,2})\.(\d{1,2})$',
                r'^(\d{1,2})\.(\d{1,2})\.(\d{2,4})$',
                r'^(\d{1,2})\.(\d{1,2})\.(\d{2,4})-(\d{1,2})\.(\d{1,2})\.(\d{2,4})$')

        return 'FOR_A_DATE' if any([re.search(reg_expr, request) for reg_expr in regs]) else 'OTHER'

    # Reversed keys and values in dictionary
    request_code = {v: k for k, v in KEYBOARD.items()}.get(request, is_date_request_or_other())
    core.MetricsManager.track(user.get_id(), request_code, user_group)

    core.log(message.chat, '> {}'.format(message.text))

    if request == KEYBOARD['TODAY'] or request == '\U0001F4D7 Сьогодні':  # Today

        today = datetime.date.today().strftime('%d.%m.%Y')

        timetable_data = get_timetable(group=user_group, user_id=user.get_id(), sdate=today, edate=today)

        if timetable_data:

            timetable_for_today = render_day_timetable(timetable_data[0], show_current=True, user_id=user.get_id())
            if len(timetable_for_today) > 4090:
                timetable_for_today = timetable_for_today[:4040] + '\n\n\U000026A0<b> ' \
                                                                   'Весь текст не помістився ' \
                                                                   'у повідомлені \U000026A0</b>'
            bot.send_message(user.get_id(), timetable_for_today, parse_mode='HTML', reply_markup=keyboard)

        elif isinstance(timetable_data, list) and not len(timetable_data):
            timetable_for_today = 'Сьогодні пар немає'
            bot_send_message_and_post_check_group(user.get_id(), timetable_for_today, user_group)
            return

    elif request == KEYBOARD['TOMORROW']:  # Tomorrow

        tomorrow = datetime.date.today() + datetime.timedelta(days=1)
        tom_day = tomorrow.strftime('%d.%m.%Y')

        timetable_data = get_timetable(group=user_group, sdate=tom_day, edate=tom_day, user_id=user.get_id())

        if timetable_data:
            timetable_for_tomorrow = render_day_timetable(timetable_data[0], user_id=user.get_id())
            if len(timetable_for_tomorrow) > 4090:
                timetable_for_tomorrow = timetable_for_tomorrow[:4040] + '\n\n\U000026A0<b> ' \
                                                                         'Весь текст не помістився ' \
                                                                         'у повідомлені \U000026A0</b>'
            bot.send_message(user.get_id(), timetable_for_tomorrow, parse_mode='HTML', reply_markup=keyboard)

        elif isinstance(timetable_data, list) and not len(timetable_data):
            timetable_for_tomorrow = 'Завтра пар немає'
            bot_send_message_and_post_check_group(user.get_id(), timetable_for_tomorrow, user_group)
            return

    elif request == KEYBOARD['FOR_A_WEEK']:  # For a week

        if datetime.date.today().isoweekday() in (5, 6, 7):  # пт, сб, нд

            timetable_for_week = ''
            today = datetime.date.today()
            current_week_day_number = today.isoweekday()
            diff_between_saturday_and_today = 6 - current_week_day_number
            next_week_first_day = today + datetime.timedelta(days=diff_between_saturday_and_today + 2)
            next_week_last_day = today + datetime.timedelta(days=diff_between_saturday_and_today + 7)

            timetable_data = get_timetable(group=user_group, sdate=next_week_first_day.strftime('%d.%m.%Y'),
                                           edate=next_week_last_day.strftime('%d.%m.%Y'), user_id=user.get_id())

            if timetable_data:
                send_or_split_by_separate_messages(timetable_data, user)
                return

            elif isinstance(timetable_data, list) and not len(timetable_data):
                timetable_for_week = "На тиждень, з {} по {} пар не знайдено.".format(
                    next_week_first_day.strftime('%d.%m'), next_week_last_day.strftime('%d.%m'))

                bot_send_message_and_post_check_group(user.get_id(), timetable_for_week, user_group)
                return

        else:
            week_type_keyboard = telebot.types.InlineKeyboardMarkup()
            week_type_keyboard.row(
                *[telebot.types.InlineKeyboardButton(text=name, callback_data=name) for
                  name in ['\U00002B07 Поточний', '\U000027A1 Наступний']]
            )

            bot.send_message(user.get_id(), 'На який тиждень?', reply_markup=week_type_keyboard)

    elif request == KEYBOARD['HELP']:

        str_timetable_list = core.get_str_timetable_list()

        msg = '\U0001F552 <b>Час пар:</b>\n'
        for number, lesson in enumerate(str_timetable_list):
            msg += f'{emoji_numbers[number+1]} - {lesson}\n'

        help_text = open(os.path.join(settings.BASE_DIR, 'data', 'help.txt'), 'r', encoding="utf-8")
        msg += "\n\U0001F4C6 <b>Для пошуку по датам:</b>\n<i>15.05</i>\n<i>15.05-22.05</i>\n<i>1.1.18-10.1.18</i>\n\n" \
               "<b>Твоя група:</b> <code>{}</code> (\U0001F465 {})\n\n" \
               "{}" \
               "<b>Розробник:</b> @Koocherov\n".format(user.get_group(),
                                                       user.get_users_count_from_group(),
                                                       help_text.read())

        help_text.close()

        help_kb = telebot.types.InlineKeyboardMarkup()
        help_kb.row(
            telebot.types.InlineKeyboardButton(KEYBOARD['MAIN_MENU'], callback_data=KEYBOARD['MAIN_MENU'])
        )
        help_kb.row(
            telebot.types.InlineKeyboardButton(KEYBOARD['CHANGE_GROUP'], callback_data=KEYBOARD['CHANGE_GROUP'])
        )

        kb = telebot.types.ReplyKeyboardMarkup(resize_keyboard=True, one_time_keyboard=True)
        kb.row(KEYBOARD['MAIN_MENU'])
        kb.row(KEYBOARD['CHANGE_GROUP'])

        bot.send_message(message.chat.id, msg, reply_markup=help_kb, parse_mode='HTML')

    elif request == KEYBOARD['FOR_A_GROUP']:
        sent = bot.send_message(message.chat.id,
                                'Для того щоб подивитись розклад будь якої групи на тиждень введи її назву')
        bot.register_next_step_handler(sent, show_other_group)

    elif request == KEYBOARD['ADS']:

        ads_kb = telebot.types.ReplyKeyboardMarkup(resize_keyboard=True, one_time_keyboard=True)

        if core.AdService.check_if_user_have_ad(user.get_id()):
            ads_kb.row(KEYBOARD['AD_DEL'])
        else:
            ads_kb.row(KEYBOARD['AD_ADD'])
        ads_kb.row(KEYBOARD['MAIN_MENU'])

        ads_stat = core.MetricsManager.get_statistics_by_types_during_the_week().get('ADS', 'хз')

        rendered_ads = core.AdService.render_ads()

        msg = 'Переглядів за тиждень: {} \U0001F440\n\n{}'.format(ads_stat, rendered_ads)

        sent = bot.send_message(user.get_id(), msg, parse_mode='HTML', reply_markup=ads_kb)

        bot.register_next_step_handler(sent, process_menu)

    elif request == KEYBOARD['FOR_A_TEACHER']:

        user_saved_teachers = core.Teachers.get_user_saved_teachers(user.get_id())

        if not user_saved_teachers:
            m = 'Для того щоб подивитись розклад викладача на поточний тиждень - введи його прізвище.'
            sent = bot.send_message(message.chat.id, m)
            bot.register_next_step_handler(sent, select_teacher_by_second_name)

        else:
            last_teachers_kb = telebot.types.InlineKeyboardMarkup()
            for teacher in user_saved_teachers:
                last_teachers_kb.row(
                    telebot.types.InlineKeyboardButton('\U0001F464 ' + teacher, callback_data=teacher[:30]),
                )

            last_teachers_kb.row(
                telebot.types.InlineKeyboardButton('\U0001F50D Ввести прізвище', callback_data='Ввести прізвище')
            )

            msg = 'Вибери викладача із списку або натисни "\U0001F50D Ввести прізвище"'
            bot.send_message(user.get_id(), msg, reply_markup=last_teachers_kb)

    elif re.search(r'^(\d{1,2})\.(\d{1,2})$', request):

        date = request + '.' + str(datetime.date.today().year)
        timetable_data = get_timetable(group=user_group, edate=date, sdate=date, user_id=user.get_id())

        if timetable_data:
            timetable_for_date = render_day_timetable(timetable_data[0], user_id=user.get_id())

        elif isinstance(timetable_data, list) and not len(timetable_data):
            msg = 'Щоб подивитися розклад на конкретний день, введи дату в такому форматі:' \
                  '\n<b>05.03</b> або <b>5.3</b>\nПо кільком дням: \n<b>5.03-15.03</b>\n' \
                  '\nДата вводиться без пробілів (день.місяць)<b> рік вводити не треба</b> ' \

            timetable_for_date = 'На <b>{}</b>, для групи <b>{}</b> пар не знайдено.\n\n{}'.format(date,
                                                                                                   user_group,
                                                                                                   msg)
        else:
            return

        bot.send_message(message.chat.id, timetable_for_date, parse_mode='HTML', reply_markup=keyboard)

    elif re.search(r'^(\d{1,2})\.(\d{1,2})-(\d{1,2})\.(\d{1,2})$', request):

        s_date = message.text.split('-')[0] + '.' + str(datetime.date.today().year)
        e_date = message.text.split('-')[1] + '.' + str(datetime.date.today().year)
        timetable_days = get_timetable(group=user_group, sdate=s_date, edate=e_date, user_id=user.get_id())

        if isinstance(timetable_days, list) and not len(timetable_days):
            msg = 'Щоб подивитися розклад на конкретний день, введи дату в такому форматі:' \
                  '\n<b>05.03</b> або <b>5.3</b>\nПо кільком дням: \n<b>5.03-15.03</b>\n' \
                  '\nДата вводиться без пробілів (день.місяць)<b> рік вводити не треба</b> '
            timetable_for_days = 'На <b>{} - {}</b>, для групи <b>{}</b> пар не знайдено.\n\n{}'.format(s_date,
                                                                                                        e_date,
                                                                                                        user_group,
                                                                                                        msg)

            bot.send_message(user.get_id(), timetable_for_days, parse_mode='HTML', reply_markup=keyboard)
            return

        if timetable_days:
            send_or_split_by_separate_messages(timetable_days, user)

    elif re.search(r'^(\d{1,2})\.(\d{1,2})\.(\d{2,4})$', request):

        date = request
        timetable_data = get_timetable(group=user_group, edate=date, sdate=date, user_id=user.get_id())

        if timetable_data:
            timetable_for_date = render_day_timetable(timetable_data[0], user_id=user.get_id())
        elif isinstance(timetable_data, list) and not len(timetable_data):
            msg = 'Щоб подивитися розклад на конкретний день, введи дату в такому форматі:' \
                  '\n<b>05.03</b> або <b>5.3</b>\nПо кільком дням: \n<b>5.03-15.03</b>\n' \
                  '\nДата вводиться без пробілів (день.місяць)<b> рік вводити не треба</b> ' \

            timetable_for_date = 'На <b>{}</b>, для групи <b>{}</b> пар не знайдено.\n\n{}'.format(date,
                                                                                                   user_group,
                                                                                                   msg)

        bot.send_message(message.chat.id, timetable_for_date, parse_mode='HTML', reply_markup=keyboard)

    elif re.search(r'^(\d{1,2})\.(\d{1,2})\.(\d{2,4})-(\d{1,2})\.(\d{1,2})\.(\d{2,4})$', request):

        s_date = request.split('-')[0]
        e_date = request.split('-')[1]
        timetable_days = get_timetable(group=user_group, sdate=s_date, edate=e_date, user_id=user.get_id())

        if isinstance(timetable_days, list) and not len(timetable_days):
            msg = 'Щоб подивитися розклад на конкретний день, введи дату в такому форматі:' \
                  '\n<b>05.03</b> або <b>5.3</b>\nПо кільком дням: \n<b>5.03-15.03</b>\n' \
                  '\nДата вводиться без пробілів (день.місяць)<b> рік вводити не треба</b> '
            timetable_for_days = f'На <b>{s_date} - {e_date}</b>, для групи <b>{user_group}</b> ' \
                                 f'пар не знайдено.\n\n{msg}'

            bot.send_message(user.get_id(), timetable_for_days, parse_mode='HTML', reply_markup=keyboard)
            return

        if timetable_days:
            send_or_split_by_separate_messages(timetable_days, user)

    elif any(map(str.isdigit, request)):

        msg = '\U0001F446 Якщо ти хочеш подивитися розклад по датам - вводь дату у форматі <b>ДЕНЬ.МІСЯЦЬ</b>\n' \
              '\n  <i>наприклад:</i>\n  <i>15.5</i>\n  <i>15.5-22.5</i>\n  <i>1.1.18-10.1.18</i>\n\n' \
              '\U0001F446 Щоб змінити групу, зайди в {}'.format(KEYBOARD['HELP'])

        bot.send_message(user.get_id(), msg, parse_mode='HTML', reply_markup=keyboard)

    elif request == KEYBOARD['MAIN_MENU']:
        bot.send_message(user.get_id(), 'Ок', reply_markup=keyboard)

    elif 'якую' in request or 'пасибо' in request or 'thank' in request:
        bot.send_message(user.get_id(), 'будь-ласка)', reply_markup=keyboard)

    elif core.is_group_valid(request):
        msg = 'Якщо ти хочеш змінити групу, тоді зайди в пункт меню {}'.format(KEYBOARD['HELP'])
        bot.send_message(user.get_id(), text=msg, reply_markup=keyboard)

    elif request[-1] == '?':
        answers = ['да', 'хз', 'ноу', 'думаю ні']
        bot.send_message(user.get_id(), random.choice(answers), reply_markup=keyboard)

    else:
        answers = ['м?', 'хм.. \U0001F914', 'не розумію(', 'вибери потрібне в меню', 'моя твоя не понімать', 'wot?']
        bot.send_message(user.get_id(), random.choice(answers), reply_markup=keyboard)


def main():

    core.DBManager.create_db_tables()

    bot.delete_webhook()

    core.log(msg='Запуск...')
    bot.polling(interval=settings.POLLING_INTERVAL, timeout=5)


if __name__ == "__main__":
    app.run(debug=True) if len(sys.argv) > 1 else main()
