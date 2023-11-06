def load_messages():
    """
    加载聊天记录
    :return: 聊天记录
    """
    check_session(session)
    success, message = auth(request.headers, session)
    code = 200  # 200表示云端存储了 node.js改写时若云端不存储则返回201
    if not success:
        return {"code": code, "data": [{"role": "web-system", "content": message}]}
    if session.get('user_id') is None:
        messages_history = [{"role": "assistant", "content": project_info},
                            {"role": "assistant", "content": "#### 当前浏览器会话为首次请求\n"
                                                             "#### 请输入已有用户`id`或创建新的用户`id`。\n"
                                                             "- 已有用户`id`请在输入框中直接输入\n"
                                                             "- 创建新的用户`id`请在输入框中输入`new:xxx`,其中`xxx`为你的自定义id，请牢记\n"
                                                             "- 输入`帮助`以获取帮助提示"}]
    else:
        user_info = get_user_info(session.get('user_id'))
        chat_id = user_info['selected_chat_id']
        messages_history = user_info['chats'][chat_id]['messages_history']
        chat_name = user_info['chats'][chat_id]['name']
        logger.warning(f"用户({session.get('user_id')})加载“{chat_name}”对话的聊天记录，共{len(messages_history)}条记录")
    return {"code": code, "data": messages_history, "message": ""}


@app.route('/downloadUserDictFile', methods=['GET', 'POST'])
def download_user_dict_file():
    """
    下载用户字典文件
    :return: 用户字典文件
    """
    check_session(session)
    admin_password = request.headers.get("admin-password")
    if admin_password is None:
        success, message = auth(request.headers, session)
        if not success:
            return "未授权，无法下载"
        user_id = request.headers.get("user-id")
        if user_id is None:
            return "未绑定用户，无法下载"
        select_user_dict = LRUCache(USER_SAVE_MAX)
        with lock:
            select_user_dict.put(user_id, all_user_dict.get(user_id))
        # 存储为临时文件再发送出去
        with tempfile.NamedTemporaryFile(suffix='.pkl', delete=False, mode='wb') as temp_file:
            # 将 Python 对象使用 pickle 序列化保存到临时文件中
            pickle.dump(select_user_dict, temp_file)
            response = make_response(send_file(temp_file.name, as_attachment=True))
            response.headers["Content-Disposition"] = f"attachment; filename={user_id}_of_{USER_DICT_FILE}"
            response.call_on_close(lambda: os.remove(temp_file.name))
            return response
    else:
        if admin_password != ADMIN_PASSWORD:
            return "管理员密码错误，无法下载"
        response = make_response(send_file(os.path.join(DATA_DIR, USER_DICT_FILE), as_attachment=True))
        response.headers["Content-Disposition"] = f"attachment; filename={USER_DICT_FILE}"
        return response


def backup_user_dict_file():
    """
    备份用户字典文件
    :return:
    """
    backup_file_name = USER_DICT_FILE.replace(".pkl",
                                              f"_buckup_{datetime.datetime.now().strftime('%Y%m%d%H%M%S%f')}.pkl")
    shutil.copy(os.path.join(DATA_DIR, USER_DICT_FILE), os.path.join(DATA_DIR, backup_file_name))
    logger.warning(f"备份用户字典文件{USER_DICT_FILE}为{backup_file_name}")


@app.route('/uploadUserDictFile', methods=['POST'])
def upload_user_dict_file():
    """
    上传用户字典文件 并合并记录
    :return:
    """
    check_session(session)
    file = request.files.get('file')  # 获取上传的文件
    if file:
        admin_password = request.headers.get("admin-password")
        if admin_password is None:
            success, message = auth(request.headers, session)
            if not success:
                return "未授权，无法合并用户记录"
            user_id = request.headers.get("user-id")
            if user_id is None:
                return "未绑定用户，无法合并用户记录"
            if not file.filename.endswith(".pkl"):
                return "上传文件格式错误，无法合并用户记录"

            # 读取获取的文件
            upload_user_dict = ""
            with tempfile.NamedTemporaryFile(suffix='.pkl', delete=False, mode='wb') as temp_file:
                file.save(temp_file.name)
                # 将 Python 对象使用 pickle 序列化保存到临时文件中
                try:
                    with open(temp_file.name, 'rb') as temp_file:
                        upload_user_dict = pickle.load(temp_file)
                except:
                    return "上传文件格式错误，无法解析以及合并用户记录"
            os.remove(temp_file.name)
            # 判断是否为LRUCache对象
            if not isinstance(upload_user_dict, LRUCache):
                return "上传文件格式错误，无法合并用户记录"
            with lock:
                user_info = all_user_dict.get(user_id)
            upload_user_info = upload_user_dict.get(user_id)
            if user_info is None or upload_user_info is None:
                return "仅能合并相同用户id的记录，请确保所上传的记录与当前用户id一致"
            backup_user_dict_file()
            for chat_id, chat_info in upload_user_info['chats'].items():
                if user_info['chats'].get(chat_id) is None:
                    user_info['chats'][chat_id] = chat_info
                else:
                    new_chat_id = str(uuid.uuid1())
                    user_info['chats'][new_chat_id] = chat_info
            asyncio.run(save_all_user_dict())
            return '个人用户记录合并完成'
        else:
            if admin_password != ADMIN_PASSWORD:
                return "管理员密码错误，无法上传用户记录"
            if not file.filename.endswith(".pkl"):
                return "上传文件格式错误，无法上传用户记录"
            # 读取获取的文件
            upload_user_dict = ""
            with tempfile.NamedTemporaryFile(suffix='.pkl', delete=False, mode='wb') as temp_file:
                file.save(temp_file.name)
                # 将 Python 对象使用 pickle 序列化保存到临时文件中
                try:
                    with open(temp_file.name, 'rb') as temp_file:
                        upload_user_dict = pickle.load(temp_file)
                except:
                    return "上传文件格式错误，无法解析以及合并用户记录"
            os.remove(temp_file.name)
            # 判断是否为LRUCache对象
            if not isinstance(upload_user_dict, LRUCache):
                return "上传文件格式错误，无法合并用户记录"
            backup_user_dict_file()
            with lock:
                for user_id, user_info in upload_user_dict.items():
                    if all_user_dict.get(user_id) is None:
                        all_user_dict.put(user_id, user_info)
                    else:
                        for chat_id, chat_info in user_info['chats'].items():
                            if all_user_dict.get(user_id)['chats'].get(chat_id) is None:
                                all_user_dict.get(user_id)['chats'][chat_id] = chat_info
                            else:
                                new_chat_id = str(uuid.uuid1())
                                all_user_dict.get(user_id)['chats'][new_chat_id] = chat_info
            asyncio.run(save_all_user_dict())
            return '所有用户记录合并完成'
    else:
        return '文件上传失败'
