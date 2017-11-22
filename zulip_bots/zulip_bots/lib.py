from __future__ import print_function

import json
import logging
import os
import signal
import sys
import time
import re

from six.moves import configparser

from contextlib import contextmanager

if False:
    from mypy_extensions import NoReturn
from typing import Any, Optional, List, Dict, IO, Text, Set
from types import ModuleType

from zulip import Client, ZulipError

def exit_gracefully(signum, frame):
    # type: (int, Optional[Any]) -> None
    sys.exit(0)

def get_bots_directory_path():
    # type: () -> str
    current_dir = os.path.dirname(os.path.abspath(__file__))
    return os.path.join(current_dir, 'bots')

class RateLimit(object):
    def __init__(self, message_limit, interval_limit):
        # type: (int, int) -> None
        self.message_limit = message_limit
        self.interval_limit = interval_limit
        self.message_list = []  # type: List[float]
        self.error_message = '-----> !*!*!*MESSAGE RATE LIMIT REACHED, EXITING*!*!*! <-----\n'
        'Is your bot trapped in an infinite loop by reacting to its own messages?'

    def is_legal(self):
        # type: () -> bool
        self.message_list.append(time.time())
        if len(self.message_list) > self.message_limit:
            self.message_list.pop(0)
            time_diff = self.message_list[-1] - self.message_list[0]
            return time_diff >= self.interval_limit
        else:
            return True

    def show_error_and_exit(self):
        # type: () -> NoReturn
        logging.error(self.error_message)
        sys.exit(1)

class StateHandlerError(Exception):
    pass

class StateHandler(object):
    def __init__(self, client):
        # type: (Client) -> None
        self._client = client
        self.marshal = lambda obj: json.dumps(obj)
        self.demarshal = lambda obj: json.loads(obj)
        response = self._client.get_storage()
        if response['result'] == 'success':
            self.state_ = response['state']
            self._modified_entries = set()  # type: Set[Text]
        else:
            raise StateHandlerError("Error initializing state: {}".format(str(response)))

    def put(self, key, value):
        # type: (Text, Text) -> None
        self.state_[key] = self.marshal(value)
        self._modified_entries.add(key)

    def get(self, key):
        # type: (Text) -> Text
        return self.demarshal(self.state_[key])

    def contains(self, key):
        # type: (Text) -> bool
        return key in self.state_

    def _save(self):
        # type: () -> None
        state_update = {'state': {key: self.state_[key] for key in self._modified_entries}}
        if state_update:
            response = self._client.update_storage(state_update)
            if response['result'] == 'success':
                self._modified_entries.clear()
            else:
                raise StateHandlerError("Error updating state: {}".format(str(response)))

class ExternalBotHandler(object):
    def __init__(self, client, root_dir, bot_details={}):
        # type: (Client, str, Dict[str, Any]) -> None
        # Only expose a subset of our Client's functionality
        try:
            user_profile = client.get_profile()
        except ZulipError as e:
            print('''
                ERROR: {}

                Have you not started the server?
                Or did you mis-specify the URL?
                '''.format(e))
            sys.exit(1)

        if user_profile.get('result') == 'error':
            msg = user_profile.get('msg', 'unknown')
            print('''
                ERROR: {}
                '''.format(msg))
            sys.exit(1)

        self._rate_limit = RateLimit(20, 5)
        self._client = client
        self._root_dir = root_dir
        self.bot_details = bot_details
        self._storage = StateHandler(client) if self.bot_details.get('uses_storage', False) else None
        try:
            self.user_id = user_profile['user_id']
            self.full_name = user_profile['full_name']
            self.email = user_profile['email']
        except KeyError:
            logging.error('Cannot fetch user profile, make sure you have set'
                          ' up the zuliprc file correctly.')
            sys.exit(1)

    @property
    def storage(self):
        # type: () -> StateHandler
        if not self._storage:
            raise AttributeError("""Bot tried to access storage, but has not enabled
storage access. To enable storage access, add
    META = {
        'uses_storage': True,
    }
to your bot handler class. Check out the incrementor
bot for an example on how to do this.
""")
        return self._storage

    def send_message(self, message):
        # type: (Dict[str, Any]) -> Dict[str, Any]
        if self._rate_limit.is_legal():
            return self._client.send_message(message)
        else:
            self._rate_limit.show_error_and_exit()

    def send_reply(self, message, response):
        # type: (Dict[str, Any], str) -> Dict[str, Any]
        if message['type'] == 'private':
            return self.send_message(dict(
                type='private',
                to=[x['email'] for x in message['display_recipient'] if self.email != x['email']],
                content=response,
            ))
        else:
            return self.send_message(dict(
                type='stream',
                to=message['display_recipient'],
                subject=message['subject'],
                content=response,
            ))

    def update_message(self, message):
        # type: (Dict[str, Any]) -> Dict[str, Any]
        if self._rate_limit.is_legal():
            return self._client.update_message(message)
        else:
            self._rate_limit.show_error_and_exit()

    def get_config_info(self, bot_name, optional=False):
        # type: (str, Optional[bool]) -> Dict[str, Any]
        conf_file_path = os.path.realpath(os.path.join(self._root_dir, bot_name + '.conf'))
        config = configparser.ConfigParser()
        try:
            with open(conf_file_path) as conf:
                config.readfp(conf)  # type: ignore # readfp->read_file in python 3, so not in stubs
        except IOError:
            if optional:
                return dict()
            raise
        return dict(config.items(bot_name))

    def open(self, filepath):
        # type: (str) -> IO[str]
        filepath = os.path.normpath(filepath)
        abs_filepath = os.path.join(self._root_dir, filepath)
        if abs_filepath.startswith(self._root_dir):
            return open(abs_filepath)
        else:
            raise PermissionError("Cannot open file \"{}\". Bots may only access "
                                  "files in their local directory.".format(abs_filepath))

def extract_query_without_mention(message, client):
    # type: (Dict[str, Any], ExternalBotHandler) -> str
    """
    If the bot is the first @mention in the message, then this function returns
    the stripped message with the bot's @mention removed.  Otherwise, it returns None.
    """
    mention = '@**' + client.full_name + '**'
    if not message['content'].startswith(mention):
        return None
    return message['content'][len(mention):].lstrip()

def is_private_message_from_another_user(message_dict, current_user_id):
    # type: (Dict[str, Any], int) -> bool
    """
    Checks whether a message dict represents a PM from another user.

    This function is used by the embedded bot system in the
    zulip/zulip project, so refactor with care.  See the comments in
    extract_query_without_mention.
    """
    if message_dict['type'] == 'private':
        return current_user_id != message_dict['sender_id']
    return False

def run_message_handler_for_bot(lib_module, quiet, config_file, bot_name):
    # type: (Any, bool, str, str) -> Any
    #
    # lib_module is of type Any, since it can contain any bot's
    # handler class. Eventually, we want bot's handler classes to
    # inherit from a common prototype specifying the handle_message
    # function.
    #
    # Set default bot_details, then override from class, if provided
    bot_details = {
        'name': bot_name.capitalize(),
        'description': "",
    }
    bot_details.update(getattr(lib_module.handler_class, 'META', {}))
    # Make sure you set up your ~/.zuliprc

    client_name = "Zulip{}Bot".format(bot_name.capitalize())

    try:
        client = Client(config_file=config_file, client=client_name)
    except configparser.Error as e:
        file_contents = open(config_file).read()
        print('\nERROR: {} seems to be broken:\n\n{}'.format(config_file, file_contents))
        print('\nMore details here:\n\n' + str(e) + '\n')
        sys.exit(1)

    bot_dir = os.path.dirname(lib_module.__file__)
    restricted_client = ExternalBotHandler(client, bot_dir, bot_details)

    message_handler = lib_module.handler_class()
    if hasattr(message_handler, 'initialize'):
        message_handler.initialize(bot_handler=restricted_client)

    if not quiet:
        print("Running {} Bot:".format(bot_details['name']))
        if bot_details['description'] != "":
            print("\n\t{}".format(bot_details['description']))
        print(message_handler.usage())

    def handle_message(message, flags):
        # type: (Dict[str, Any], List[str]) -> None
        logging.info('waiting for next message')

        # `mentioned` will be in `flags` if the bot is mentioned at ANY position
        # (not necessarily the first @mention in the message).
        is_mentioned = 'mentioned' in flags
        is_private_message = is_private_message_from_another_user(message, restricted_client.user_id)

        # Strip at-mention botname from the message
        if is_mentioned:
            # message['content'] will be None when the bot's @-mention is not at the beginning.
            # In that case, the message shall not be handled.
            message['content'] = extract_query_without_mention(message=message, client=restricted_client)
            if message['content'] is None:
                return

        if is_private_message or is_mentioned:
            message_handler.handle_message(
                message=message,
                bot_handler=restricted_client
            )
        restricted_client.storage._save()

    signal.signal(signal.SIGINT, exit_gracefully)

    logging.info('starting message handling...')

    def event_callback(event):
        # type: (Dict[str, Any]) -> None
        if event['type'] == 'message':
            handle_message(event['message'], event['flags'])

    client.call_on_each_event(event_callback, ['message'])
