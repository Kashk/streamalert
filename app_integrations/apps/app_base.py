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
from abc import ABCMeta, abstractmethod, abstractproperty
from decimal import Decimal
import time
from timeit import timeit

from app_integrations import LOGGER
from app_integrations.batcher import Batcher
from app_integrations.exceptions import AppIntegrationException, AppIntegrationConfigError

STREAMALERT_APPS = {}


def app(cls):
    """Class decorator to register all stream gatherer classes.
    This should be applied to any subclass for the GathererBase.
    """
    STREAMALERT_APPS[cls.type()] = cls


def get_app(config):
    """Return the proper app integration for this service

    Args:
        config (AppConfig): Loaded configuration with service, etc

    Returns:
        AppIntegration: Subclass of AppIntegration
    """
    try:
        return STREAMALERT_APPS[config['type']](config)
    except AppIntegrationException:
        raise
    except KeyError:
        if 'type' not in config:
            raise AppIntegrationException('The \'type\' is not defined in the config.')
        else:
            raise AppIntegrationException('App integration does not exist for type: '
                                          '{}'.format(config['type']))


class AppIntegration(object):
    """Base class for all app integrations to be implemented for various services"""
    __metaclass__ = ABCMeta
    _POLL_BUFFER_MULTIPLIER = 1.2

    def __init__(self, config):
        self._config = config
        self._batcher = Batcher(config)
        self._gathered_log_count = 0
        self._more_to_poll = False
        self._poll_count = 0
        self._last_timestamp = 0

    @classmethod
    @abstractproperty
    def service(cls):
        """Read only service property enforced on subclasses.

        Returns:
            str: The originating service name for these logs.
        """

    @classmethod
    @abstractproperty
    def _type(cls):
        """Read only log type property enforced on subclasses.

        Returns:
            str: The specific type of log (auth, admin, etc)
        """

    @classmethod
    def type(cls):
        """Returns a combination of the service and log type

        Returns:
            str: The specific type of log (duo_auth, duo_admin, google_admin, etc)
        """
        return '_'.join([cls.service(), cls._type()])

    @abstractmethod
    def required_auth_keys(self):
        """Function to get the expected keys that this service's auth dictionary
        should contain. To be implemented by subclasses

        Returns:
            dict: Required authentication keys, with optional description and
                format they should follow
        """

    @abstractmethod
    def _gather_logs(self):
        """Function for actual gathering of logs that should be implemented by all
        subclasses

        Returns:
            bool: Inidcator of successful processing
        """

    @abstractmethod
    def _sleep_seconds(self):
        """Function for retrieving the amount of time this service should sleep before
        trying to perform another poll. This should be implemented by all subclasses

        Returns:
            int: Number of seconds the polling function should sleep for
        """

    def _sleep(self):
        """Function to sleep the looping"""
        # Do not sleep if this is the first poll
        if self._poll_count == 0:
            LOGGER.debug('Skipping sleep for first poll')
            return

        # Sleep for n seconds so the called API does not return a bad response
        sleep_for_secs = self._sleep_seconds()
        LOGGER.debug('Sleeping \'%s\' app for %d seconds...', self.type(), sleep_for_secs)

        time.sleep(sleep_for_secs)

    def _initialize(self):
        """Method for performing any startup steps, like setting state to running"""
        # Perform another safety check to make sure this is not being invoked already
        if self._config.is_running:
            LOGGER.error('App already running for service \'%s\'.', self.type())
            return False

        LOGGER.info('App starting for service \'%s\'.', self.type())

        # Validate the auth in the config. This raises an exception upon failure
        self._validate_auth()

        self._last_timestamp = self._config.last_timestamp

        # Mark this app as running, which updates the parameter store
        self._config.mark_running()

        return True

    def _finalize(self):
        """Method for performing any final steps, like saving applicable state"""
        self._config.mark_success()

        if self._last_timestamp == self._config.start_last_timestamp:
            LOGGER.error('Ending last timestamp is the same as the beginning last timestamp')

        LOGGER.info('App complete for service \'%s\'. Gathered %d logs in %d polls.',
                    self.type(), self._gathered_log_count, self._poll_count)

        self._config.last_timestamp = self._last_timestamp

    def _check_http_response(self, response):
        """Method for checking for a valid HTTP response code"""
        success = response is not None and (200 <= response.status_code <= 299)

        if response is not None and not success:
            LOGGER.error('HTTP request failed for service \'%s\': [%d] %s',
                         self.type(),
                         response.status_code,
                         response.json()['message'])

        return success

    def _validate_auth(self):
        """Method for validating the authentication dictionary retrieved from
        AWS Parameter Store

        Returns:
            bool: Inidcator of successful validation
        """
        if not self._config:
            raise AppIntegrationConfigError('Config for service \'{}\' is empty', self.type())

        if not 'auth' in self._config:
            raise AppIntegrationConfigError('Auth config for service \'{}\' is empty', self.type())

        # Get the required authentication keys from the subclass and make sure they exist
        required_keys = set(self.required_auth_keys())
        auth_key_diff = required_keys.difference(set(self._config['auth']))
        if not auth_key_diff:
            return

        missing_auth_keys = ', '.join('\'{}\''.format(key) for key in auth_key_diff)
        raise AppIntegrationConfigError('Auth config for service \'{}\' is missing the following '
                                        'required keys: {}'.format(self.type(), missing_auth_keys))

    def _gather(self):
        """Protected entry point for the beginning of polling"""

        # Make this request sleep if the API throttles requests
        self._sleep()
        def do_gather():
            """Perform the gather using this scoped method so we can time it"""
            logs = self._gather_logs()

            # Make sure there are logs, this can be False if there was an issue polling
            if not logs:
                LOGGER.error('Gather process for service \'%s\' was not able to poll any logs',
                             self.type())
                return

            # Increment the count of logs gathered
            self._gathered_log_count += len(logs)

            # Utilize the batcher to send logs to the rule processor
            self._batcher.send_logs(self._config['function_name'], logs)

            LOGGER.debug('Updating config last timestamp from %d to %d',
                         self._config.last_timestamp, self._last_timestamp)

            # Save the config's last timestamp after each function run
            self._config.last_timestamp = self._last_timestamp

            self._poll_count += 1

        # Use timeit to track how long one poll takes, and cast to a decimal.
        # Use decimal since these floating point values can be very small and the
        # builtin float uses scientific notation when handling very small values
        exec_time = Decimal(timeit(do_gather, number=1))

        LOGGER.debug('Gather process for \'%s\' executed in %f seconds.', self.type(), exec_time)

        # Add a 20% buffer to the time it too to account for some unforeseen delay
        # Cast this back to float so general arithemtic works
        return float(exec_time * Decimal(self._POLL_BUFFER_MULTIPLIER))

    def gather(self):
        """Public method for actual gathering of logs"""
        # Initialize, saving state to 'running'
        if not self._initialize():
            return

        while self._gather() + self._sleep_seconds() < self._config.remaining_ms() / 1000.0:
            if not self._more_to_poll:
                break

            # Reset the boolean indicating that there is more data to poll. Subclasses should
            # set this to 'True' within their implementation of the '_gather_logs' function
            self._more_to_poll = not self._more_to_poll

        # Finalize, saving state to 'succeeded'
        self._finalize()
