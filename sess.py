#sess.py
from log_util import init_logger
import uuid
import os
def setup_logger():
    global logger
    logger = init_logger()
def check_session(current_session):
    """
    检查session，如果不存在则创建新的session
    :param current_session: 当前session
    :return: 当前session
    """
    global logger
    session_id = current_session.get('session_id')
    if session_id is not None:
        logger.debug("existing session, session_id:\t{}".format(session_id))
    else:
        session_id = str(uuid.uuid1())
        current_session['session_id'] = session_id
        logger.info("new session, session_id:\t{}".format(session_id))
    return session_id