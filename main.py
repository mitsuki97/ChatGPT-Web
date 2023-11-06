#main.py
import datetime
import json
import re
import shutil
import tempfile
import urllib.parse

import requests
from flask import Flask, render_template, request, session, send_file, make_response
import os
import uuid
from LRU_cache import LRUCache
from log_util import init_logger
import threading
import pickle
import asyncio
import yaml
from user import check_user_bind
# from user import get_user_info
from sess import check_session
from log_util import init_logger
import os
from typing import List, Dict, Optional, Generator
import sess
sess.setup_logger()
# 目前需要设置代理才可以访问 api
os.environ["HTTP_PROXY"] = "127.0.0.1:8888"
os.environ["HTTPS_PROXY"] = "127.0.0.1:8888"


app = Flask(__name__)
app.config['SECRET_KEY'] = os.urandom(12)



# 适配python3.6
loop = asyncio.get_event_loop()
def asyncio_run(func):
    loop.run_until_complete(func)
DATA_DIR = "data"
logger = init_logger(file_name=os.path.join(DATA_DIR, "running.log"), stdout=True)
#配置文件导入
with open(os.path.join(DATA_DIR, "config.yaml"), "r", encoding="utf-8") as f:
    config = yaml.load(f, Loader=yaml.FullLoader)
    if 'HTTPS_PROXY' in config:
        if os.environ.get('HTTPS_PROXY') is None:  # 优先使用环境变量中的代理，若环境变量中没有代理，则使用配置文件中的代理
            os.environ['HTTPS_PROXY'] = config['HTTPS_PROXY']
    if 'PASSWORD' in config:
        PASSWORD = config['PASSWORD']
    else:
        PASSWORD = ""  # 即不使用访问密码
    if 'ADMIN_PASSWORD' in config:
        ADMIN_PASSWORD = config['ADMIN_PASSWORD']
    else:
        ADMIN_PASSWORD = ""
    PORT = config['PORT']
    API_KEY = config['OPENAI_API_KEY']
    CHAT_CONTEXT_NUMBER_MAX = config[
        'CHAT_CONTEXT_NUMBER_MAX']  # 连续对话模式下的上下文最大数量 n，即开启连续对话模式后，将上传本条消息以及之前你和GPT对话的n-1条消息
    USER_SAVE_MAX = config['USER_SAVE_MAX']  # 设置最多存储n个用户，当用户过多时可适当调大

if os.getenv("DEPLOY_ON_RAILWAY") is not None or os.getenv("DEPLOY_ON_ZEABUR"):  # 如果是云部署，需要删除代理
    os.environ.pop('HTTPS_PROXY', None)

API_KEY = os.getenv("OPENAI_API_KEY", default=API_KEY)  # 如果环境变量中设置了OPENAI_API_KEY，则使用环境变量中的OPENAI_API_KEY
PORT = os.getenv("PORT", default=PORT)  # 如果环境变量中设置了PORT，则使用环境变量中的PORT
PASSWORD = os.getenv("PASSWORD", default=PASSWORD)  # 如果环境变量中设置了PASSWORD，则使用环境变量中的PASSWORD
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", default=ADMIN_PASSWORD)  # 如果环境变量中设置了ADMIN_PASSWORD，则使用环境变量中的ADMIN_PASSWORD
if ADMIN_PASSWORD == "":
    ADMIN_PASSWORD = PASSWORD  # 如果ADMIN_PASSWORD为空，则使用PASSWORD

STREAM_FLAG = True  # 是否开启流式推送
USER_DICT_FILE = "all_user_dict_v3.pkl"  # 用户信息存储文件（包含版本）
lock = threading.Lock()  # 用于线程锁

project_info = "## 欢迎使用 ChatGPT 网页版"


def get_response_from_ChatGPT_API(message_context, apikey,
                                  model="gpt-3.5-turbo", temperature=0.9, presence_penalty=0, max_tokens=2000):
    """
    从ChatGPT API获取回复
    :param message_context: 上下文
    :param apikey: API KEY
    :param model: 模型
    :param temperature: 温度
    :param presence_penalty: 惩罚
    :param max_tokens: 最大token数量
    :return: 回复
    """
    if apikey is None:
        apikey = API_KEY

    header = {"Content-Type": "application/json",
              "Authorization": "Bearer " + apikey}

    data = {
        "model": model,
        "messages": message_context,
        "temperature": temperature,
        "presence_penalty": presence_penalty,
        "max_tokens": max_tokens
    }
    url = "https://api.openai.com/v1/chat/completions"

    try:
        with requests.Session() as session:
            response = session.post(url, headers=header, data=json.dumps(data))
            response.raise_for_status()  # 检查请求的响应状态，如果出现错误将会抛出异常
            response_data = response.json()  # 获取响应的JSON数据

            # 判断是否含 choices[0].message.content
            if "choices" in response_data and response_data["choices"]:
                message = response_data["choices"][0].get("message")
                if message and "content" in message:
                    data = message["content"]
            else:
                data = str(response_data)

    except requests.exceptions.RequestException as e:
        logger.error(e)
        return str(e)

    return str(data)


def get_message_context(message_history: List[Dict[str, str]], have_chat_context: bool,
                        chat_with_history: bool) -> List[Dict[str, str]]:   
    """
    根据聊天记录生成消息上下文
    :param message_history: 聊天记录列表
    :param have_chat_context: 是否有聊天上下文
    :param chat_with_history: 是否需要获取历史聊天记录
    :return: 消息上下文列表
    """
    message_context = []
    total = 0
    if chat_with_history:
        num = min(len(message_history), CHAT_CONTEXT_NUMBER_MAX, have_chat_context)
        # 获取所有有效聊天记录
        valid_start = 0
        valid_num = 0
        for i, message in enumerate(message_history[::-1]):
            if message['role'] in {'assistant', 'user'}:
                valid_start = len(message_history) - i - 1
                valid_num += 1
            if valid_num >= num:
                break

        message_context = [message for message in message_history[valid_start:] if message['role'] in {'assistant', 'user'}]
        total = sum(len(message['content']) for message in message_context)
    else:
        message_context.append(message_history[-1])
        total = len(message_history[-1]['content'])

    logger.info(f"len(message_context): {len(message_context)} total: {total}")
    return message_context


def handle_messages_get_response(message: str, apikey: str, message_history: List[Dict[str, str]],
                                 have_chat_context: bool, chat_with_history: bool) -> str:
    """
    处理用户发送的消息，获取回复
    :param message: 用户发送的消息
    :param apikey:
    :param message_history: 消息历史
    :param have_chat_context: 已发送消息数量上下文(从重置为连续对话开始)
    :param chat_with_history: 是否连续对话
    """
    message_history.append({"role": "user", "content": message})
    _, message_context = get_message_context(message_history, have_chat_context, chat_with_history)
    response = get_response_from_ChatGPT_API(message_context, apikey)
    message_history.append({"role": "assistant", "content": response})

    return response


def get_response_stream_generate_from_ChatGPT_API(message_context: List[Dict[str, str]], apikey: str,
                                                  message_history: List[Dict[str, str]],
                                                  model: str = "gpt-3.5-turbo", temperature: float = 0.9,
                                                  presence_penalty: int = 0, max_tokens: int = 2000) -> Generator[str, None, None]:
    """
    从ChatGPT API获取回复
    :param apikey:
    :param message_context: 上下文
    :param message_history: 消息历史
    :param model: 模型
    :param temperature: 温度
    :param presence_penalty: 惩罚
    :param max_tokens: 最大token数量
    :return: 回复生成器
    """
    if apikey is None:
        apikey = API_KEY

    header = {"Content-Type": "application/json",
              "Authorization": "Bearer " + apikey}

    data = {
        "model": model,
        "temperature": temperature,
        "presence_penalty": presence_penalty,
        "max_tokens": max_tokens,
        "messages": message_context,
        "stream": True
    }
    logger.info("开始流式请求")
    url = "https://api.openai.com/v1/chat/completions"
    # 请求接收流式数据 动态print
    try:
        response = requests.request("POST", url, headers=header, json=data, stream=True)

        def generate() -> Generator[str, None, None]:
            stream_content = str()
            one_message = {"role": "assistant", "content": stream_content}
            message_history.append(one_message)
            i = 0
            for line in response.iter_lines():
                line_str = str(line, encoding='utf-8')
                if line_str.startswith("data:"):
                    if line_str.startswith("data: [DONE]"):
                        asyncio_run(save_all_user_dict())
                        logger.info("用户得到的回复内容：{}...".format(one_message["content"][:200]))
                        break
                    line_json = json.loads(line_str[5:])
                    if 'choices' in line_json:
                        if len(line_json['choices']) > 0:
                            choice = line_json['choices'][0]
                            if 'delta' in choice:
                                delta = choice['delta']
                                if 'role' in delta:
                                    role = delta['role']
                                elif 'content' in delta:
                                    delta_content = delta['content']
                                    i += 1
                                    if i < 40:
                                        print(delta_content, end="")
                                    elif i == 40:
                                        print("......")
                                    one_message['content'] = one_message['content'] + delta_content
                                    yield delta_content

                elif len(line_str.strip()) > 0:
                    print(line_str)
                    yield line_str

    except Exception as e:
        ee = e

        def generate() -> Generator[str, None, None]:
            yield "request error:\n" + str(ee)

    return generate


def handle_messages_get_response_stream(message: str, apikey: str, message_history: List[Dict[str, str]],
                                        have_chat_context: bool, chat_with_history: bool) -> Generator[str, None, None]:
    message_history.append({"role": "user", "content": message})
    asyncio_run(save_all_user_dict())
    message_context = get_message_context(message_history, have_chat_context, chat_with_history)
    _, generate = get_response_stream_generate_from_ChatGPT_API(message_context, apikey, message_history)
    return generate

@app.route('/', methods=['GET', 'POST'])
def index():
    """
    主页
    :return: 主页
    """
    check_session(session)
    return render_template('index.html')


@app.route('/loadHistory', methods=['GET', 'POST'])


def auth(request_head, session):
    """
    验证用户身份
    :param request_head: 请求头
    :param session: session
    :return: 验证结果
    """
    user_id = request_head.get("user-id")
    user_id = urllib.parse.unquote(user_id)
    password = request_head.get("password")
    apikey = request_head.get("api-key")

    user_info = get_user_info(user_id)
    if len(PASSWORD) > 0 and password != PASSWORD:
        return False, "访问密码错误，请在设置中填写正确的访问密码"

    if user_info is not None:
        session['user_id'] = user_id
        if apikey is not None and len(apikey) > 1 and apikey != "null" and apikey != "undefined":
            user_info['apikey'] = apikey
        else:
            user_info['apikey'] = None
        return True, "success"
    else:
        if session.get('user_id') is not None:
            del session['user_id']
        return False, "用户不存在，请在设置中填写正确的用户id，或发送new:xxx创建新的用户，其中xxx为你的自定义id"


@app.route('/loadChats', methods=['GET', 'POST'])
def load_chats():
    """
    加载聊天联系人
    :return: 聊天联系人
    """
    check_session(session)
    success, message = auth(request.headers, session)

    if not check_user_bind(session) or not success:
        chats = []
    else:
        user_info = get_user_info(session.get('user_id'))
        chats = []
        chat_ids = []
        # 如果置顶聊天列表不为空
        if "chat_sticky_list" in user_info:
            for chat_id in user_info['chat_sticky_list']:
                if user_info['chats'].get(chat_id) is not None:
                    chat_ids.append(chat_id)
        
        for chat_id in user_info['chats'].keys():
            if chat_id not in chat_ids:
                chat_ids.append(chat_id)

        user_info['chat_sticky_list'] = chat_ids

        for i, chat_id in enumerate(chat_ids):
            chat_info = user_info['chats'][chat_id]
            if chat_info['chat_with_history']:
                mode = "continuous"
            else:
                mode = "normal"

            if "assistant_prompt" in chat_info:
                assistant_prompt = chat_info['assistant_prompt']
            else:
                assistant_prompt = ""
            if "context_size" not in chat_info:
                chat_info['context_size'] = 5
            if "context_have" not in chat_info:
                chat_info["context_have"] = 1
            chats.append(
                {"id": chat_id, "name": chat_info['name'], "selected": chat_id == user_info['selected_chat_id'],
                 "assistant_prompt": assistant_prompt, "context_size": chat_info['context_size'],
                 "context_have": chat_info["context_have"], "sticky_number": i,
                 "mode": mode, "messages_total": len(user_info['chats'][chat_id]['messages_history'])})
    code = 200  # 200表示云端存储了 node.js改写时若云端不存储则返回201
    return {"code": code, "data": chats, "message": ""}


def new_chat_dict(user_id, name, send_time):
    return {"chat_with_history": False,
            "have_chat_context": 0,  # 从每次重置聊天模式后开始重置一次之后累计
            "name": name,
            "messages_history": [{"role": "assistant", "content": project_info},
                                 {"role": "web-system", "content": f"当前对话的用户id为{user_id}"},
                                 {"role": "web-system", "content": send_time},
                                 {"role": "web-system", "content": f"你已添加了{name}，现在可以开始聊天了。"},
                                 ]}


def new_user_dict(user_id, send_time):
    chat_id = str(uuid.uuid1())
    user_dict = {"chats": {chat_id: new_chat_dict(user_id, "默认对话", send_time)},
                 "selected_chat_id": chat_id,
                 "default_chat_id": chat_id}

    user_dict['chats'][chat_id]['messages_history'].insert(1, {"role": "assistant",
                                                               "content": "创建新的用户id成功，请牢记该id"})
    return user_dict


def get_balance(apikey):
    head = ""
    if apikey is not None:
        head = "###  用户专属api key余额  \n"
    else:
        head = "### 通用api key  \n"
        apikey = API_KEY

    subscription_url = "https://api.openai.com/v1/dashboard/billing/subscription"
    headers = {
        "Authorization": "Bearer " + apikey,
        "Content-Type": "application/json"
    }
    subscription_response = requests.get(subscription_url, headers=headers)
    if subscription_response.status_code == 200:
        data = subscription_response.json()
        total = data.get("hard_limit_usd")
    else:
        return head + subscription_response.text

    # start_date设置为今天日期前99天
    start_date = (datetime.datetime.now() - datetime.timedelta(days=99)).strftime("%Y-%m-%d")
    # end_date设置为今天日期+1
    end_date = (datetime.datetime.now() + datetime.timedelta(days=1)).strftime("%Y-%m-%d")
    billing_url = f"https://api.openai.com/v1/dashboard/billing/usage?start_date={start_date}&end_date={end_date}"
    billing_response = requests.get(billing_url, headers=headers)
    if billing_response.status_code == 200:
        data = billing_response.json()
        total_usage = data.get("total_usage") / 100
        daily_costs = data.get("daily_costs")
        days = min(5, len(daily_costs))
        recent = f"##### 最近{days}天使用情况  \n"
        for i in range(days):
            cur = daily_costs[-i - 1]
            date = datetime.datetime.fromtimestamp(cur.get("timestamp")).strftime("%Y-%m-%d")
            line_items = cur.get("line_items")
            cost = 0
            for item in line_items:
                cost += item.get("cost")
            recent += f"\t{date}\t{cost / 100} \n"
    else:
        return head + billing_response.text

    return head + f"\n#### 总额:\t{total:.4f}  \n" \
                  f"#### 已用:\t{total_usage:.4f}  \n" \
                  f"#### 剩余:\t{total - total_usage:.4f}  \n" \
                  f"\n" + recent


@app.route('/returnMessage', methods=['GET', 'POST'])
def return_message():
    """
    获取用户发送的消息，调用get_chat_response()获取回复，返回回复，用于更新聊天框
    :return:
    """
    check_session(session)
    request_data = request.get_json()

    success, message = auth(request.headers, session)
    if not success:
        session.clear()

    messages = request_data.get("messages")
    max_tokens = request_data.get("max_tokens")
    model = request_data.get("model")
    temperature = request_data.get("temperature")
    stream = request_data.get("stream")
    continuous_chat = request_data.get("continuous_chat")
    save_message = request_data.get("save_message")

    send_message = messages[-1].get("content")
    send_time = messages[-1].get("send_time")
    display_time = bool(messages[-1].get("display_time"))
    url_redirect = {"url_redirect": "/", "user_id": None}
    if send_message == "帮助":
        return "### 帮助\n" \
               "1. 输入`new:xxx`创建新的用户id\n " \
               "2. 输入`id:your_id`切换到已有用户id，新会话时无需加`id:`进入已有用户\n" \
               "3. 输入`rename_id:xxx`可将当前用户id更改\n" \
               "4. 输入`查余额`可获得余额信息及最近几天使用量\n" \
               "5. 相关设置也可以在设置面板中进行设置\n" \
               "6. 输入`帮助`查看帮助信息"
    if session.get('user_id') is None:  # 如果当前session未绑定用户
        logger.warning("当前会话为首次请求，用户输入:\t"+send_message)
        if send_message.startswith("new:"):
            user_id = send_message.split(":")[1]
            url_redirect["user_id"] = user_id
            if user_id in all_user_dict:
                session['user_id'] = user_id
                return url_redirect
            user_dict = new_user_dict(user_id, send_time)
            lock.acquire()
            all_user_dict.put(user_id, user_dict)  # 默认普通对话
            lock.release()
            logger.warning(f"创建新的用户id:\t{user_id}")
            session['user_id'] = user_id
            url_redirect["user_id"] = user_id
            return url_redirect
        else:
            user_id = send_message
            user_info = get_user_info(user_id)
            if user_info is None:
                logger.warning(f"用户输入的id{user_id}不存在")
                return "用户id不存在，请重新输入或创建新的用户id"
            else:
                session['user_id'] = user_id
                logger.warning(f"切换到已有用户id:\t{user_id}")
                # 重定向到index
                url_redirect["user_id"] = user_id
                return url_redirect
    else:  # 当存在用户id时
        if send_message.startswith("id:"):
            user_id = send_message.split(":")[1].strip()
            user_info = get_user_info(user_id)
            if user_info is None:
                logger.warning(f"用户尝试切换的的id{user_id}不存在")
                return "用户id不存在，请重新输入或创建新的用户id"
            else:
                session['user_id'] = user_id
                url_redirect["user_id"] = user_id
                logger.warning(f"切换到已有用户id:\t{user_id}")
                # 重定向到index
                return url_redirect
        elif send_message.startswith("new:"):
            user_id = send_message.split(":")[1]
            if user_id in all_user_dict:
                return "用户id已存在，请重新输入或切换到已有用户id"
            session['user_id'] = user_id
            url_redirect["user_id"] = user_id
            user_dict = new_user_dict(user_id, send_time)
            lock.acquire()
            all_user_dict.put(user_id, user_dict)
            lock.release()
            logger.warning(f"创建新的用户id:\t{user_id}")
            return url_redirect
        elif send_message.startswith("delete:"):  # 删除用户
            user_id = send_message.split(":")[1]
            if user_id != session.get('user_id'):
                logger.warning(f"用户({session.get('user_id')})尝试删除用户id({user_id})")
                return "只能删除当前会话的用户id"
            else:
                lock.acquire()
                all_user_dict.delete(user_id)
                lock.release()
                session['user_id'] = None
                logger.warning(f"删除用户id:\t{user_id}成功")
                # 异步存储all_user_dict
                asyncio_run(save_all_user_dict())
                return url_redirect
        elif send_message.startswith("set_apikey:"):
            apikey = send_message.split(":")[1]
            user_info = get_user_info(session.get('user_id'))
            user_info['apikey'] = apikey
            # TODO 前端未存储
            logger.info(f"设置用户专属apikey:\t{apikey}")
            return "设置用户专属apikey成功"
        elif send_message.startswith("rename_id:"):
            new_user_id = send_message.split(":")[1]
            user_info = get_user_info(session.get('user_id'))
            if new_user_id in all_user_dict:
                return "用户id已存在，请重新输入"
            else:
                lock.acquire()
                all_user_dict.delete(session['user_id'])
                all_user_dict.put(new_user_id, user_info)
                lock.release()
                session['user_id'] = new_user_id
                asyncio_run(save_all_user_dict())
                logger.warning(f"修改用户id:\t{new_user_id}")
                url_redirect["user_id"] = new_user_id
                return url_redirect
        elif send_message == "查余额":
            user_info = get_user_info(session.get('user_id'))
            apikey = user_info.get('apikey')
            return get_balance(apikey)
        else:  # 处理聊天数据
            user_id = session.get('user_id')
            logger.info(f"用户({user_id})发送消息:{send_message}")
            user_info = get_user_info(user_id)
            chat_id = user_info['selected_chat_id']
            messages_history = user_info['chats'][chat_id]['messages_history']
            chat_with_history = user_info['chats'][chat_id]['chat_with_history']
            apikey = user_info.get('apikey')
            if chat_with_history:
                user_info['chats'][chat_id]['have_chat_context'] += 1
            if display_time:
                messages_history.append({'role': 'web-system', "content": send_time})
            for m in messages:
                keys = list(m.keys())
                for k in keys:
                    if k not in ['role', 'content']:
                        del m[k]
            if not STREAM_FLAG:
                if save_message:
                    messages_history.append(messages[-1])
                response = get_response_from_ChatGPT_API(messages, apikey)
                if save_message:
                    messages_history.append({"role": "assistant", "content": response})
                asyncio_run(save_all_user_dict())

                logger.info(f"用户({session.get('user_id')})得到的回复消息:{response[:40]}...")
                # 异步存储all_user_dict
                asyncio_run(save_all_user_dict())
                return response
            else:
                if save_message:
                    messages_history.append(messages[-1])
                asyncio_run(save_all_user_dict())
                if not save_message:
                    messages_history = []
                generate = get_response_stream_generate_from_ChatGPT_API(messages, apikey, messages_history,
                                                                         model=model, temperature=temperature,
                                                                         max_tokens=max_tokens)
                return app.response_class(generate(), mimetype='application/json')


async def save_all_user_dict():
    """
    异步存储all_user_dict
    :return:
    """
    await asyncio.sleep(0)
    lock.acquire()
    with open(os.path.join(DATA_DIR, USER_DICT_FILE), "wb") as f:
        pickle.dump(all_user_dict, f)
    logger.debug("聊天j存储成功")
    lock.release()


@app.route('/selectChat', methods=['GET'])
def select_chat():
    """
    选择聊天对象
    :return:
    """
    chat_id = request.args.get("id")
    check_session(session)
    if not check_user_bind(session):
        return {"code": -1, "msg": "请先创建或输入已有用户id"}
    user_id = session.get('user_id')
    user_info = get_user_info(user_id)
    user_info['selected_chat_id'] = chat_id
    return {"code": 200, "msg": "选择聊天对象成功"}


@app.route('/newChat', methods=['GET'])
def new_chat():
    """
    新建聊天对象
    :return:
    """
    name = request.args.get("name")
    time = request.args.get("time")
    new_chat_id = request.args.get("chat_id")
    check_session(session)
    if not check_user_bind(session):
        return {"code": -1, "msg": "请先创建或输入已有用户id"}
    user_id = session.get('user_id')
    user_info = get_user_info(user_id)
    # new_chat_id = str(uuid.uuid1())
    user_info['selected_chat_id'] = new_chat_id
    user_info['chats'][new_chat_id] = new_chat_dict(user_id, name, time)
    user_info["chat_sticky_list"].insert(1, new_chat_id)
    logger.info("新建聊天对象")
    asyncio_run(save_all_user_dict())
    return {"code": 200, "data": {"name": name, "id": new_chat_id, "selected": True,
                                  "messages_total": len(user_info['chats'][new_chat_id]['messages_history'])}}


@app.route('/deleteHistory', methods=['GET'])
def delete_history():
    """
    清空上下文
    :return:
    """
    check_session(session)
    if not check_user_bind(session):
        logger.info("请先创建或输入已有用户id")
        return {"code": -1, "msg": "请先创建或输入已有用户id"}
    user_info = get_user_info(session.get('user_id'))
    chat_id = user_info['selected_chat_id']
    default_chat_id = user_info['default_chat_id']
    if default_chat_id == chat_id:
        logger.warning("清空历史记录")
        user_info["chats"][chat_id]['messages_history'] = user_info["chats"][chat_id]['messages_history'][:5]
    else:
        logger.warning("删除聊天对话")
        del user_info["chats"][chat_id]
    user_info['selected_chat_id'] = default_chat_id
    return "2"


@app.route('/editChat', methods=['GET', 'POST'])
def edit_chat():
    check_session(session)
    if not check_user_bind(session):
        return {"code": -1, "msg": "请先创建或输入已有用户id"}
    user_id = session.get('user_id')
    user_info = get_user_info(user_id)
    data = request.get_json()
    id = data.get("id")
    if id not in user_info["chats"]:
        return {"code": -1, "msg": "需要编辑的聊天对话不存在"}
    if data.get("name") is not None:
        user_info["chats"][id]["name"] = data.get("name")
    if data.get("context_size") is not None:
        user_info["chats"][id]["context_size"] = data.get("context_size")  # 每次发送请求时，发送的上下文数量
    if data.get("mode") is not None:
        mode = data.get("mode")
        if mode == "normal":
            user_info["chats"][id]['chat_with_history'] = False
        else:
            user_info["chats"][id]['chat_with_history'] = True
    if data.get("assistant_prompt") is not None:
        assistant_prompt = data.get("assistant_prompt")
        user_info["chats"][id]["assistant_prompt"] = assistant_prompt

    if data.get("context_have") is not None:
        context_have = data.get("context_have")
        user_info["chats"][id]["context_have"] = context_have

    # 如果需要置顶该聊天
    if data.get("sticky_number") is not None and id != user_info['default_chat_id']:
        sticky_number = data.get("sticky_number")
        if "chat_sticky_list" not in user_info:
            user_info["chat_sticky_list"] = []
        if sticky_number < 1:
            user_info["chat_sticky_list"].remove(id)
        else:
            if id in user_info["chat_sticky_list"]:
                user_info["chat_sticky_list"].remove(id)
            user_info["chat_sticky_list"].insert(sticky_number, id)

    return {"code": 200, "msg": "修改成功"}


def check_load_pickle():

    data_files = os.listdir(DATA_DIR)
    have_move = False  # 匹配新版迁移，新版本的用户记录移到了data目录中
    for file in data_files:
        if re.match(r"all_user_dict_.*\.pkl", file):
            have_move = True
            break

    if not have_move:
        files = os.listdir()
        for file in files:
            if re.match(r"all_user_dict_.*\.pkl", file):
                shutil.move(file, DATA_DIR)

    if os.path.exists(os.path.join(DATA_DIR, USER_DICT_FILE)):
        with open(os.path.join(DATA_DIR, USER_DICT_FILE), "rb") as pickle_file:
            all_user_dict = pickle.load(pickle_file)
            all_user_dict.change_capacity(USER_SAVE_MAX)
        logger.warning(f"已加载上次存储的用户上下文，共有{len(all_user_dict)}用户, 分别是")
        for i, user_id in enumerate(list(all_user_dict.keys())):
            info = f"{i} 用户id:{user_id}\t对话统计:\t"
            user_info = all_user_dict.get(user_id)
            for chat_id in user_info['chats'].keys():
                info += f"{user_info['chats'][chat_id]['name']}[{len(user_info['chats'][chat_id]['messages_history'])}] "
            logger.info(info)
    elif os.path.exists(os.path.join(DATA_DIR, "all_user_dict_v2.pkl")):  # 适配V2
        logger.warning('检测到v2版本的上下文，将转换为v3版本')
        with open(os.path.join(DATA_DIR, "all_user_dict_v2.pkl"), "rb") as pickle_file:
            all_user_dict = pickle.load(pickle_file)
            all_user_dict.change_capacity(USER_SAVE_MAX)
        logger.warning(f"共有用户个{len(all_user_dict)}")
        for user_id in list(all_user_dict.keys()):
            user_info: dict = all_user_dict.get(user_id)
            for chat_id in user_info['chats'].keys():
                if "messages_history" in user_info['chats'][chat_id]:
                    for i in range(len(user_info['chats'][chat_id]['messages_history'])):
                        # 将system关键字改为 web-system
                        if "role" in user_info['chats'][chat_id]['messages_history'][i] and \
                                user_info['chats'][chat_id]['messages_history'][i]["role"] == "system":
                            user_info['chats'][chat_id]['messages_history'][i]["role"] = "web-system"

        asyncio_run(save_all_user_dict())

    elif os.path.exists(os.path.join(DATA_DIR, "all_user_dict.pkl")):  # 适配V1版本
        logger.warning('检测到v1版本的上下文，将转换为v3版本')
        with open(os.path.join(DATA_DIR, "all_user_dict.pkl"), "rb") as pickle_file:
            all_user_dict = pickle.load(pickle_file)
            all_user_dict.change_capacity(USER_SAVE_MAX)
        logger.warning(f"共有用户{len(all_user_dict)}个")
        for user_id in list(all_user_dict.keys()):
            user_info: dict = all_user_dict.get(user_id)
            if "messages_history" in user_info:
                user_dict = new_user_dict(user_id, "")
                chat_id = user_dict['selected_chat_id']
                user_dict['chats'][chat_id]['messages_history'] = user_info['messages_history']
                user_dict['chats'][chat_id]['chat_with_history'] = user_info['chat_with_history']
                user_dict['chats'][chat_id]['have_chat_context'] = user_info['have_chat_context']
                all_user_dict.put(user_id, user_dict)  # 更新
        asyncio_run(save_all_user_dict())
    else:
        with open(os.path.join(DATA_DIR, USER_DICT_FILE), "wb") as pickle_file:
            pickle.dump(all_user_dict, pickle_file)
        logger.warning("未检测到上次存储的用户上下文，已创建新的用户上下文")

    # 判断all_user_dict是否为None且时LRUCache的对象
    if all_user_dict is None or not isinstance(all_user_dict, LRUCache):
        logger.warning("all_user_dict为空或不是LRUCache对象，已创建新的LRUCache对象")
def get_user_info(user_id):
    """
    获取用户信息
    :param user_id: 用户id
    :return: 用户信息
    """
    with lock:
        LRUCache(USER_SAVE_MAX)
        user_info = all_user_dict.get(user_id)
    return user_info


if __name__ == '__main__' or __name__ == 'main':
    logger.warning("持久化存储文件路径为:{}".format(os.path.join(os.getcwd(), os.path.join(DATA_DIR, USER_DICT_FILE))))
    
    global all_user_dict
    all_user_dict = LRUCache(USER_SAVE_MAX)
    check_load_pickle()

    if len(API_KEY) == 0:
        # 退出程序
        logger.error("请在openai官网注册账号，获取api_key填写至程序内或命令行参数中")
        exit()
    if os.getenv("DEPLOY_ON_ZEABUR") is None:
        app.run(host="0.0.0.0", port=PORT, debug=False)