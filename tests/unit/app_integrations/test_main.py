"""
Copyright 2017-present, Airbnb Inc.

Licensed under the Apache License, Version 2.0 (the "License");
you may not use this file except in compliance with the License.
You may obtain a copy of the License at

   http://www.apache.org/licenses/LICENSE-2.0

Unless required by applicable law or agreed to in writing, software
distributed under the License is distributed on an "AS IS" BASIS,
WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
See the License for the specific language governing permissions and
limitations under the License.
"""
# pylint: disable=protected-access
from mock import patch

from nose.tools import raises

from app_integrations.config import AppConfig
from app_integrations.exceptions import (
    AppIntegrationConfigError,
    AppIntegrationException
)
from app_integrations.main import handler
from tests.unit.app_integrations.test_helpers import (
    get_mock_context,
    get_valid_config_dict,
    MockSSMClient
)


@patch.object(AppConfig, 'SSM_CLIENT', MockSSMClient())
@raises(AppIntegrationConfigError)
def test_handler():
    """App Integration - Test Handler"""
    handler(None, get_mock_context())


@patch.object(AppConfig, 'SSM_CLIENT', MockSSMClient())
@raises(AppIntegrationException)
@patch('app_integrations.config.AppConfig.mark_failure')
@patch('app_integrations.config.AppConfig.load_config')
def test_handler_bad_type(config_mock, failure_mock):
    """App Integration - Test Handler, Bad Service Type"""
    base_config = get_valid_config_dict()
    base_config.update({'type': 'bad_type', 'current_state': 'running'})
    config_mock.return_value = AppConfig(base_config)
    handler(None, get_mock_context())

    failure_mock.assert_called()
