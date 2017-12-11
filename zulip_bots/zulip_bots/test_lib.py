#!/usr/bin/env python

from __future__ import absolute_import
from __future__ import print_function

import os

import json
import logging
import mock
import requests
import unittest

from mock import MagicMock, patch, call

from zulip_bots.lib import StateHandler
import zulip_bots.lib
from six.moves import zip

from contextlib import contextmanager
from importlib import import_module
from unittest import TestCase

from typing import List, Dict, Any, Optional, Callable, Tuple
from types import ModuleType

from zulip_bots.simple_lib import (
    SimpleStorage,
    SimpleMessageServer,
)

class StubBotHandler:
    def __init__(self):
        # type: () -> None
        self.storage = SimpleStorage()
        self.message_server = SimpleMessageServer()
        self.reset_transcript()

    def reset_transcript(self):
        # type: () -> None
        self.transcript = []  # type: List[Tuple[str, Dict[str, Any]]]

    def send_message(self, message):
        # type: (Dict[str, Any]) -> Dict[str, Any]
        self.transcript.append(('send_message', message))
        return self.message_server.send(message)

    def send_reply(self, message, response):
        # type: (Dict[str, Any], str) -> Dict[str, Any]
        response_message = dict(
            content=response
        )
        self.transcript.append(('send_reply', response_message))
        return self.message_server.send(response_message)

    def update_message(self, message):
        # type: (Dict[str, Any]) -> None
        self.message_server.update(message)

    def get_config_info(self, bot_name, optional=False):
        # type: (str, bool) -> Dict[str, Any]
        return None

    def unique_reply(self):
        # type: () -> Dict[str, Any]
        responses = [
            message
            for (method, message)
            in self.transcript
            if method == 'send_reply'
        ]
        self.ensure_unique_response(responses)
        return responses[0]

    def unique_response(self):
        # type: () -> Dict[str, Any]
        responses = [
            message
            for (method, message)
            in self.transcript
        ]
        self.ensure_unique_response(responses)
        return responses[0]

    def ensure_unique_response(self, responses):
        # type: (List[Dict[str, Any]]) -> None
        if not responses:
            raise Exception('The bot is not responding for some reason.')
        if len(responses) > 1:
            raise Exception('The bot is giving too many responses for some reason.')

class StubBotTestCase(TestCase):
    '''
    The goal for this class is to eventually replace
    BotTestCase for places where we may want more
    fine-grained control and less heavy setup.
    '''

    bot_name = ''

    def _get_handlers(self):
        # type: () -> Tuple[Any, StubBotHandler]
        bot = get_bot_message_handler(self.bot_name)
        bot_handler = StubBotHandler()

        if hasattr(bot, 'initialize'):
            bot.initialize(bot_handler)

        return (bot, bot_handler)

    def get_response(self, message):
        # type: (Dict[str, Any]) -> Dict[str, Any]
        bot, bot_handler = self._get_handlers()
        bot_handler.reset_transcript()
        bot.handle_message(message, bot_handler)
        return bot_handler.unique_response()

    def verify_reply(self, request, response):
        # type: (str, str) -> None

        bot, bot_handler = self._get_handlers()

        message = dict(
            sender_email='foo@example.com',
            sender_full_name='Foo Test User',
            content=request,
        )
        bot_handler.reset_transcript()
        bot.handle_message(message, bot_handler)
        reply = bot_handler.unique_reply()
        self.assertEqual(response, reply['content'])

    def verify_dialog(self, conversation):
        # type: (List[Tuple[str, str]]) -> None

        # Start a new message handler for the full conversation.
        bot, bot_handler = self._get_handlers()

        for (request, expected_response) in conversation:
            message = dict(
                display_recipient='foo_stream',
                sender_email='foo@example.com',
                sender_full_name='Foo Test User',
                content=request,
            )
            bot_handler.reset_transcript()
            bot.handle_message(message, bot_handler)
            response = bot_handler.unique_response()
            self.assertEqual(expected_response, response['content'])

    def test_bot_usage(self):
        # type: () -> None
        bot = get_bot_message_handler(self.bot_name)
        self.assertNotEqual(bot.usage(), '')

    def test_bot_responds_to_empty_message(self) -> None:
        message = dict(
            sender_email='foo@example.com',
            display_recipient='foo',
            content='',
        )

        # get_response will fail if we don't respond at all
        response = self.get_response(message)

        # we also want a non-blank response
        self.assertTrue(len(response['content']) >= 1)

    def mock_http_conversation(self, test_name):
        # type: (str) -> Any
        assert test_name is not None
        http_data = read_bot_fixture_data(self.bot_name, test_name)
        return mock_http_conversation(http_data)

    def mock_request_exception(self):
        # type: () -> Any
        return mock_request_exception()

    def mock_config_info(self, config_info):
        # type: (Dict[str, str]) -> Any
        return patch('zulip_bots.test_lib.StubBotHandler.get_config_info', return_value=config_info)

def get_bot_message_handler(bot_name):
    # type: (str) -> Any
    # message_handler is of type 'Any', since it can contain any bot's
    # handler class. Eventually, we want bot's handler classes to
    # inherit from a common prototype specifying the handle_message
    # function.
    lib_module = import_module('zulip_bots.bots.{bot}.{bot}'.format(bot=bot_name))  # type: Any
    return lib_module.handler_class()

def read_bot_fixture_data(bot_name, test_name):
    # type: (str, str) -> Dict[str, Any]
    base_path = os.path.realpath(os.path.join(os.path.dirname(
        os.path.abspath(__file__)), 'bots', bot_name, 'fixtures'))
    http_data_path = os.path.join(base_path, '{}.json'.format(test_name))
    with open(http_data_path) as f:
        content = f.read()
    http_data = json.loads(content)
    return http_data

@contextmanager
def mock_http_conversation(http_data):
    # type: (Dict[str, Any]) -> Any
    """
    Use this context manager to mock and verify a bot's HTTP
    requests to the third-party API (and provide the correct
    third-party API response. This allows us to test things
    that would require the Internet without it).

    http_data should be fixtures data formatted like the data
    in zulip_bots/zulip_bots/bots/giphy/fixtures/test_normal.json
    """
    def get_response(http_response, http_headers):
        # type: (Dict[str, Any], Dict[str, Any]) -> Any
        """Creates a fake `requests` Response with a desired HTTP response and
        response headers.
        """
        mock_result = requests.Response()
        mock_result._content = json.dumps(http_response).encode()  # type: ignore # This modifies a "hidden" attribute.
        mock_result.status_code = http_headers.get('status', 200)
        return mock_result

    def assert_called_with_fields(mock_result, http_request, fields):
        # type: (Any, Dict[str, Any], List[str]) -> None
        """Calls `assert_called_with` on a mock object using an HTTP request.
        Uses `fields` to determine which keys to look for in HTTP request and
        to test; if a key is in `fields`, e.g., 'headers', it will be used in
        the assertion.
        """
        args = {}

        for field in fields:
            if field in http_request:
                args[field] = http_request[field]

        mock_result.assert_called_with(http_request['api_url'], **args)

    http_request = http_data.get('request')
    http_response = http_data.get('response')
    http_headers = http_data.get('response-headers')
    http_method = http_request.get('method', 'GET')

    if http_method == 'GET':
        with patch('requests.get') as mock_get:
            mock_get.return_value = get_response(http_response, http_headers)
            yield
            assert_called_with_fields(
                mock_get,
                http_request,
                ['params', 'headers']
            )
    else:
        with patch('requests.post') as mock_post:
            mock_post.return_value = get_response(http_response, http_headers)
            yield
            assert_called_with_fields(
                mock_post,
                http_request,
                ['params', 'headers', 'json']
            )

@contextmanager
def mock_request_exception():
    # type: () -> Any
    def assert_mock_called(mock_result):
        # type: (Any) -> None
        mock_result.assert_called()

    with patch('requests.get') as mock_get:
        mock_get.return_value = True
        mock_get.side_effect = requests.exceptions.RequestException
        yield
        assert_mock_called(mock_get)
