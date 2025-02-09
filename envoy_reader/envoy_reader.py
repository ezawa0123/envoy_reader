"""Module to read production and consumption values from an Enphase Envoy on the local network."""
import argparse
import asyncio
import datetime
import logging
import jwt
import re
import time
from json.decoder import JSONDecodeError

import httpx
from bs4 import BeautifulSoup
from envoy_utils.envoy_utils import EnvoyUtils

#
# Legacy parser is only used on ancient firmwares
#
PRODUCTION_REGEX = r"<td>Currentl.*</td>\s+<td>\s*(\d+|\d+\.\d+)\s*(W|kW|MW)</td>"
DAY_PRODUCTION_REGEX = r"<td>Today</td>\s+<td>\s*(\d+|\d+\.\d+)\s*(Wh|kWh|MWh)</td>"
WEEK_PRODUCTION_REGEX = (
    r"<td>Past Week</td>\s+<td>\s*(\d+|\d+\.\d+)\s*(Wh|kWh|MWh)</td>"
)
LIFE_PRODUCTION_REGEX = (
    r"<td>Since Installation</td>\s+<td>\s*(\d+|\d+\.\d+)\s*(Wh|kWh|MWh)</td>"
)
SERIAL_REGEX = re.compile(r"Envoy\s*Serial\s*Number:\s*([0-9]+)")

ENDPOINT_URL_PRODUCTION_JSON = "http{}://{}/production.json"
ENDPOINT_URL_PRODUCTION_V1 = "http{}://{}/api/v1/production"
ENDPOINT_URL_PRODUCTION_INVERTERS = "http{}://{}/api/v1/production/inverters"
ENDPOINT_URL_PRODUCTION = "http{}://{}/production"
ENDPOINT_URL_CHECK_JWT = "https://{}/auth/check_jwt"

# pylint: disable=pointless-string-statement

ENVOY_MODEL_S = "PC"
ENVOY_MODEL_C = "P"
ENVOY_MODEL_LEGACY = "P0"

LOGIN_URL = "https://entrez.enphaseenergy.com/login"
TOKEN_URL = "https://entrez.enphaseenergy.com/entrez_tokens"

_LOGGER = logging.getLogger(__name__)


def has_production_and_consumption(json):
    """Check if json has keys for both production and consumption."""
    return "production" in json and "consumption" in json


def has_metering_setup(json):
    """Check if Active Count of Production CTs (eim) installed is greater than one."""
    return json["production"][1]["activeCount"] > 0


class SwitchToHTTPS(Exception):
    pass


class EnvoyReader:  # pylint: disable=too-many-instance-attributes
    """Instance of EnvoyReader"""

    # P0 for older Envoy model C, s/w < R3.9 no json pages
    # P for production data only (ie. Envoy model C, s/w >= R3.9)
    # PC for production and consumption data (ie. Envoy model S)

    message_battery_not_available = (
        "Battery storage data not available for your Envoy device."
    )

    message_consumption_not_available = (
        "Consumption data not available for your Envoy device."
    )

    def __init__(  # pylint: disable=too-many-arguments
        self,
        host,
        username="envoy",
        password="",
        inverters=False,
        async_client=None,
        enlighten_user=None,
        enlighten_pass=None,
        commissioned=False,
        enlighten_site_id=None,
        enlighten_serial_num=None,
        https_flag="",
        token=""
    ):
        """Init the EnvoyReader."""
        self.host = host.lower()
        self.username = username
        self.password = password
        self.get_inverters = inverters
        self.endpoint_type = None
        self.serial_number_last_six = None
        self.endpoint_production_json_results = None
        self.endpoint_production_v1_results = None
        self.endpoint_production_inverters = None
        self.endpoint_production_results = None
        self.isMeteringEnabled = False  # pylint: disable=invalid-name
        self._async_client = async_client
        self._authorization_header = None
        self.enlighten_user = enlighten_user
        self.enlighten_pass = enlighten_pass
        self.commissioned = commissioned
        self.enlighten_site_id = enlighten_site_id
        self.enlighten_serial_num = enlighten_serial_num
        self.https_flag = https_flag
        self.token_auth = bool(token)
        self._token = token

    @property
    def async_client(self):
        """Return the httpx client."""
        return self._async_client or httpx.AsyncClient(verify=False)

    async def _update(self):
        """Update the data."""
        if self.endpoint_type == ENVOY_MODEL_S:
            await self._update_from_pc_endpoint()
        if self.endpoint_type == ENVOY_MODEL_C or (
            self.endpoint_type == ENVOY_MODEL_S and not self.isMeteringEnabled
        ):
            await self._update_from_p_endpoint()
        if self.endpoint_type == ENVOY_MODEL_LEGACY:
            await self._update_from_p0_endpoint()

    async def _update_from_pc_endpoint(self):
        """Update from PC endpoint."""
        await self._update_endpoint(
            "endpoint_production_json_results", ENDPOINT_URL_PRODUCTION_JSON
        )

    async def _update_from_p_endpoint(self):
        """Update from P endpoint."""
        await self._update_endpoint(
            "endpoint_production_v1_results", ENDPOINT_URL_PRODUCTION_V1
        )

    async def _update_from_p0_endpoint(self):
        """Update from P0 endpoint."""
        await self._update_endpoint(
            "endpoint_production_results", ENDPOINT_URL_PRODUCTION
        )

    async def _update_endpoint(self, attr, url):
        """Update a property from an endpoint."""
        formatted_url = url.format(self.https_flag, self.host)
        response = await self._async_fetch_with_retry(
            formatted_url, follow_redirects=False
        )
        setattr(self, attr, response)

    async def _async_fetch_with_retry(self, url, **kwargs):
        """Retry 3 times to fetch the url if there is a transport error."""
        for attempt in range(3):
            _LOGGER.debug(
                "HTTP GET Attempt #%s: %s: Header:%s",
                attempt + 1,
                url,
                self._authorization_header,
            )
            try:
                async with self.async_client as client:
                    resp = await client.get(
                        url, headers=self._authorization_header, timeout=30, **kwargs
                    )
                    _LOGGER.debug("Fetched from %s: %s: %s", url, resp, resp.text)
                    return resp
            except httpx.TransportError:
                if attempt == 2:
                    raise

    async def _async_post(self, url, data, cookies=None, **kwargs):
        _LOGGER.debug("HTTP POST Attempt: %s", url)
        # _LOGGER.debug("HTTP POST Data: %s", data)
        try:
            async with self.async_client as client:
                resp = await client.post(
                    url, cookies=cookies, data=data, timeout=30, **kwargs
                )
                _LOGGER.debug("HTTP POST %s: %s: %s", url, resp, resp.text)
                _LOGGER.debug("HTTP POST Cookie: %s", resp.cookies)
                return resp
        except httpx.TransportError:  # pylint: disable=try-except-raise
            raise

    async def _getEnphaseToken(  # pylint: disable=invalid-name
        self,
    ):
        payload_login = {
            "username": self.enlighten_user,
            "password": self.enlighten_pass,
        }

        # Login to website and store cookie
        resp = await self._async_post(LOGIN_URL, data=payload_login)

        if self.commissioned == "True" or self.commissioned == "Commissioned":
            payload_token = {
                "Site": self.enlighten_site_id,
                "serialNum": self.enlighten_serial_num,
            }
            response = await self._async_post(
                TOKEN_URL, data=payload_token, cookies=resp.cookies
            )

            parsed_html = BeautifulSoup(response.text, features="html.parser")
            self._token = parsed_html.body.find(  # pylint: disable=invalid-name, unused-variable, redefined-outer-name
                "textarea"
            ).text
            _LOGGER.debug("Commissioned Token: %s", self._token)

        else:
            payload_token = {"uncommissioned": "true", "Site": ""}
            response = await self._async_post(
                TOKEN_URL, data=payload_token, cookies=resp.cookies
            )
            soup = BeautifulSoup(response.text, features="html.parser")
            self._token = soup.find("textarea").contents[
                0
            ]  # pylint: disable=invalid-name
            _LOGGER.debug("Uncommissioned Token: %s", self._token)

        # Create HTTP Header
        self._authorization_header = {"Authorization": "Bearer " + self._token}

        # Fetch the Enphase Token status from the local Envoy
        token_validation_html = await self._async_fetch_with_retry(
            ENDPOINT_URL_CHECK_JWT.format(self.host)
        )

        # Parse the HTML return from Envoy and check the text
        soup = BeautifulSoup(token_validation_html.text, features="html.parser")
        token_validation = soup.find("h2").contents[0]
        self._is_enphase_token_valid(token_validation)

    def _is_enphase_token_valid(self, response):
        if response == "Valid token.":
            _LOGGER.debug("Token is valid")
            return True
        else:
            _LOGGER.debug("Invalid token!")
            return False

    def _is_enphase_token_expired(self, token):
        decode = jwt.decode(
            token, options={"verify_signature": False}, algorithms="ES256"
        )
        exp_epoch = decode["exp"]
        exp_time = datetime.datetime.fromtimestamp(exp_epoch)
        if datetime.datetime.now() < exp_time:
            _LOGGER.debug("Token expires at: %s", exp_time)
            return False
        else:
            _LOGGER.debug("Token expired on: %s", exp_time)
            return True

    async def check_connection(self):
        """Check if the Envoy is reachable. Also check if HTTP or"""
        """HTTPS is needed."""
        _LOGGER.debug("Checking Host: %s", self.host)
        resp = await self._async_fetch_with_retry(
            ENDPOINT_URL_PRODUCTION_V1.format(self.https_flag, self.host)
        )
        _LOGGER.debug("Check connection HTTP Code: %s", resp.status_code)
        if resp.status_code == 301:
            raise SwitchToHTTPS

    async def getData(self, getInverters=True):  # pylint: disable=invalid-name
        """Fetch data from the endpoint and if inverters selected default"""
        """to fetching inverter data."""

        # Check if the Secure flag is set
        if self.https_flag == "s":
            _LOGGER.debug("Checking Token value: %s", self._token)
            # Check if a token has already been retrieved
            if self._token == "":
                _LOGGER.debug("Found empty token: %s", self._token)
                await self._getEnphaseToken()
            else:
                _LOGGER.debug("Token is populated: %s", self._token)
                if self._is_enphase_token_expired(self._token):
                    raise Exception("Token Expired")
                    _LOGGER.debug("Found Expired token - Retrieving new token")
                    await self._getEnphaseToken()

        if not self.endpoint_type:
            await self.detect_model()
        else:
            await self._update()

        if not self.get_inverters or not getInverters:
            return

        inverters_url = ENDPOINT_URL_PRODUCTION_INVERTERS.format(
            self.https_flag, self.host
        )
        inverters_auth = httpx.DigestAuth(self.username, self.password)

        response = await self._async_fetch_with_retry(
            inverters_url, auth=inverters_auth
        )
        _LOGGER.debug(
            "Fetched from %s: %s: %s",
            inverters_url,
            response,
            response.text,
        )
        if response.status_code == 401:
            response.raise_for_status()
        self.endpoint_production_inverters = response
        return

    async def detect_model(self):
        """Method to determine if the Envoy supports consumption values or only production."""
        # If a password was not given as an argument when instantiating
        # the EnvoyReader object than use the last six numbers of the serial
        # number as the password.  Otherwise use the password argument value.
        if self.password == "" and not self.serial_number_last_six:
            await self.get_serial_number()

        try:
            await self._update_from_pc_endpoint()
        except httpx.HTTPError:
            pass

        # If self.endpoint_production_json_results.status_code is set with
        # 401 then we will give an error
        if (
            self.endpoint_production_json_results
            and self.endpoint_production_json_results.status_code == 401
        ):
            raise RuntimeError(
                "Could not connect to Envoy model. "
                + "Appears your Envoy is running firmware that requires secure communcation. "
                + "Please enter in the needed Enlighten credentials during setup."
            )

        if (
            self.endpoint_production_json_results
            and self.endpoint_production_json_results.status_code == 200
            and has_production_and_consumption(
                self.endpoint_production_json_results.json()
            )
        ):
            self.isMeteringEnabled = has_metering_setup(
                self.endpoint_production_json_results.json()
            )
            if not self.isMeteringEnabled:
                await self._update_from_p_endpoint()
            self.endpoint_type = ENVOY_MODEL_S
            return

        try:
            await self._update_from_p_endpoint()
        except httpx.HTTPError:
            pass
        if (
            self.endpoint_production_v1_results
            and self.endpoint_production_v1_results.status_code == 200
        ):
            self.endpoint_type = ENVOY_MODEL_C  # Envoy-C, production only
            return

        try:
            await self._update_from_p0_endpoint()
        except httpx.HTTPError:
            pass
        if (
            self.endpoint_production_results
            and self.endpoint_production_results.status_code == 200
        ):
            self.endpoint_type = ENVOY_MODEL_LEGACY  # older Envoy-C
            return

        raise RuntimeError(
            "Could not connect or determine Envoy model. "
            + "Check that the device is up at 'http://"
            + self.host
            + "'."
        )

    async def get_serial_number(self):
        """Method to get last six digits of Envoy serial number for auth"""
        full_serial = await self.get_full_serial_number()
        if full_serial:
            gen_passwd = EnvoyUtils.get_password(full_serial, self.username)
            if self.username == "envoy" or self.username != "installer":
                self.password = self.serial_number_last_six = full_serial[-6:]
            else:
                self.password = gen_passwd

    async def get_full_serial_number(self):
        """Method to get the  Envoy serial number."""
        response = await self._async_fetch_with_retry(
            f"http{self.https_flag}://{self.host}/info.xml",
            follow_redirects=True,
        )
        if not response.text:
            return None
        if "<sn>" in response.text:
            return response.text.split("<sn>")[1].split("</sn>")[0]
        match = SERIAL_REGEX.search(response.text)
        if match:
            return match.group(1)

    def create_connect_errormessage(self):
        """Create error message if unable to connect to Envoy"""
        return (
            "Unable to connect to Envoy. "
            + "Check that the device is up at 'http://"
            + self.host
            + "'."
        )

    def create_json_errormessage(self):
        """Create error message if unable to parse JSON response"""
        return (
            "Got a response from '"
            + self.host
            + "', but metric could not be found. "
            + "Maybe your model of Envoy doesn't "
            + "support the requested metric."
        )

    async def production(self):
        """Running getData() beforehand will set self.enpoint_type and self.isDataRetrieved"""
        """so that this method will only read data from stored variables"""

        if self.endpoint_type == ENVOY_MODEL_S:
            raw_json = self.endpoint_production_json_results.json()
            idx = 1 if self.isMeteringEnabled else 0
            production = raw_json["production"][idx]["wNow"]
        elif self.endpoint_type == ENVOY_MODEL_C:
            raw_json = self.endpoint_production_v1_results.json()
            production = raw_json["wattsNow"]
        elif self.endpoint_type == ENVOY_MODEL_LEGACY:
            text = self.endpoint_production_results.text
            match = re.search(PRODUCTION_REGEX, text, re.MULTILINE)
            if match:
                if match.group(2) == "kW":
                    production = float(match.group(1)) * 1000
                else:
                    if match.group(2) == "mW":
                        production = float(match.group(1)) * 1000000
                    else:
                        production = float(match.group(1))
            else:
                raise RuntimeError("No match for production, check REGEX  " + text)
        return int(production)

    async def consumption(self):
        """Running getData() beforehand will set self.enpoint_type and self.isDataRetrieved"""
        """so that this method will only read data from stored variables"""

        """Only return data if Envoy supports Consumption"""
        if (
            self.endpoint_type in ENVOY_MODEL_C
            or self.endpoint_type in ENVOY_MODEL_LEGACY
        ):
            return self.message_consumption_not_available

        raw_json = self.endpoint_production_json_results.json()
        consumption = raw_json["consumption"][0]["wNow"]
        return int(consumption)

    async def daily_production(self):
        """Running getData() beforehand will set self.enpoint_type and self.isDataRetrieved"""
        """so that this method will only read data from stored variables"""

        if self.endpoint_type == ENVOY_MODEL_S and self.isMeteringEnabled:
            raw_json = self.endpoint_production_json_results.json()
            daily_production = raw_json["production"][1]["whToday"]
        elif self.endpoint_type == ENVOY_MODEL_C or (
            self.endpoint_type == ENVOY_MODEL_S and not self.isMeteringEnabled
        ):
            raw_json = self.endpoint_production_v1_results.json()
            daily_production = raw_json["wattHoursToday"]
        elif self.endpoint_type == ENVOY_MODEL_LEGACY:
            text = self.endpoint_production_results.text
            match = re.search(DAY_PRODUCTION_REGEX, text, re.MULTILINE)
            if match:
                if match.group(2) == "kWh":
                    daily_production = float(match.group(1)) * 1000
                else:
                    if match.group(2) == "MWh":
                        daily_production = float(match.group(1)) * 1000000
                    else:
                        daily_production = float(match.group(1))
            else:
                raise RuntimeError(
                    "No match for Day production, " "check REGEX  " + text
                )
        return int(daily_production)

    async def daily_consumption(self):
        """Running getData() beforehand will set self.enpoint_type and self.isDataRetrieved"""
        """so that this method will only read data from stored variables"""

        """Only return data if Envoy supports Consumption"""
        if (
            self.endpoint_type in ENVOY_MODEL_C
            or self.endpoint_type in ENVOY_MODEL_LEGACY
        ):
            return self.message_consumption_not_available

        raw_json = self.endpoint_production_json_results.json()
        daily_consumption = raw_json["consumption"][0]["whToday"]
        return int(daily_consumption)

    async def seven_days_production(self):
        """Running getData() beforehand will set self.enpoint_type and self.isDataRetrieved"""
        """so that this method will only read data from stored variables"""

        if self.endpoint_type == ENVOY_MODEL_S and self.isMeteringEnabled:
            raw_json = self.endpoint_production_json_results.json()
            seven_days_production = raw_json["production"][1]["whLastSevenDays"]
        elif self.endpoint_type == ENVOY_MODEL_C or (
            self.endpoint_type == ENVOY_MODEL_S and not self.isMeteringEnabled
        ):
            raw_json = self.endpoint_production_v1_results.json()
            seven_days_production = raw_json["wattHoursSevenDays"]
        elif self.endpoint_type == ENVOY_MODEL_LEGACY:
            text = self.endpoint_production_results.text
            match = re.search(WEEK_PRODUCTION_REGEX, text, re.MULTILINE)
            if match:
                if match.group(2) == "kWh":
                    seven_days_production = float(match.group(1)) * 1000
                else:
                    if match.group(2) == "MWh":
                        seven_days_production = float(match.group(1)) * 1000000
                    else:
                        seven_days_production = float(match.group(1))
            else:
                raise RuntimeError(
                    "No match for 7 Day production, " "check REGEX " + text
                )
        return int(seven_days_production)

    async def seven_days_consumption(self):
        """Running getData() beforehand will set self.enpoint_type and self.isDataRetrieved"""
        """so that this method will only read data from stored variables"""

        """Only return data if Envoy supports Consumption"""
        if (
            self.endpoint_type in ENVOY_MODEL_C
            or self.endpoint_type in ENVOY_MODEL_LEGACY
        ):
            return self.message_consumption_not_available

        raw_json = self.endpoint_production_json_results.json()
        seven_days_consumption = raw_json["consumption"][0]["whLastSevenDays"]
        return int(seven_days_consumption)

    async def lifetime_production(self):
        """Running getData() beforehand will set self.enpoint_type and self.isDataRetrieved"""
        """so that this method will only read data from stored variables"""

        if self.endpoint_type == ENVOY_MODEL_S and self.isMeteringEnabled:
            raw_json = self.endpoint_production_json_results.json()
            lifetime_production = raw_json["production"][1]["whLifetime"]
        elif self.endpoint_type == ENVOY_MODEL_C or (
            self.endpoint_type == ENVOY_MODEL_S and not self.isMeteringEnabled
        ):
            raw_json = self.endpoint_production_v1_results.json()
            lifetime_production = raw_json["wattHoursLifetime"]
        elif self.endpoint_type == ENVOY_MODEL_LEGACY:
            text = self.endpoint_production_results.text
            match = re.search(LIFE_PRODUCTION_REGEX, text, re.MULTILINE)
            if match:
                if match.group(2) == "kWh":
                    lifetime_production = float(match.group(1)) * 1000
                else:
                    if match.group(2) == "MWh":
                        lifetime_production = float(match.group(1)) * 1000000
                    else:
                        lifetime_production = float(match.group(1))
            else:
                raise RuntimeError(
                    "No match for Lifetime production, " "check REGEX " + text
                )
        return int(lifetime_production)

    async def lifetime_consumption(self):
        """Running getData() beforehand will set self.enpoint_type and self.isDataRetrieved"""
        """so that this method will only read data from stored variables"""

        """Only return data if Envoy supports Consumption"""
        if (
            self.endpoint_type in ENVOY_MODEL_C
            or self.endpoint_type in ENVOY_MODEL_LEGACY
        ):
            return self.message_consumption_not_available

        raw_json = self.endpoint_production_json_results.json()
        lifetime_consumption = raw_json["consumption"][0]["whLifetime"]
        return int(lifetime_consumption)

    async def inverters_production(self):
        """Running getData() beforehand will set self.enpoint_type and self.isDataRetrieved"""
        """so that this method will only read data from stored variables"""

        """Only return data if Envoy supports retrieving Inverter data"""
        if self.endpoint_type == ENVOY_MODEL_LEGACY:
            return None

        response_dict = {}
        try:
            for item in self.endpoint_production_inverters.json():
                response_dict[item["serialNumber"]] = [
                    item["lastReportWatts"],
                    time.strftime(
                        "%Y-%m-%d %H:%M:%S", time.localtime(item["lastReportDate"])
                    ),
                ]
        except (JSONDecodeError, KeyError, IndexError, TypeError, AttributeError):
            return None

        return response_dict

    async def battery_storage(self):
        """Return battery data from Envoys that support and have batteries installed"""
        if (
            self.endpoint_type in ENVOY_MODEL_LEGACY
            or self.endpoint_type in ENVOY_MODEL_C
        ):
            return self.message_battery_not_available

        try:
            raw_json = self.endpoint_production_json_results.json()
        except JSONDecodeError:
            return None

        """For Envoys that support batteries but do not have them installed the"""
        """percentFull will not be available in the JSON results. The API will"""
        """only return battery data if batteries are installed."""
        if "percentFull" not in raw_json["storage"][0].keys():
            return self.message_battery_not_available

        return raw_json["storage"][0]

    def run_in_console(self):
        """If running this module directly, print all the values in the console."""
        print("Reading...")
        loop = asyncio.get_event_loop()
        data_results = loop.run_until_complete(
            asyncio.gather(self.getData(), return_exceptions=False)
        )

        loop = asyncio.get_event_loop()
        results = loop.run_until_complete(
            asyncio.gather(
                self.production(),
                self.consumption(),
                self.daily_production(),
                self.daily_consumption(),
                self.seven_days_production(),
                self.seven_days_consumption(),
                self.lifetime_production(),
                self.lifetime_consumption(),
                self.inverters_production(),
                self.battery_storage(),
                return_exceptions=False,
            )
        )

        print(f"production:              {results[0]}")
        print(f"consumption:             {results[1]}")
        print(f"daily_production:        {results[2]}")
        print(f"daily_consumption:       {results[3]}")
        print(f"seven_days_production:   {results[4]}")
        print(f"seven_days_consumption:  {results[5]}")
        print(f"lifetime_production:     {results[6]}")
        print(f"lifetime_consumption:    {results[7]}")
        if "401" in str(data_results):
            print(
                "inverters_production:    Unable to retrieve inverter data - Authentication failure"
            )
        elif results[8] is None:
            print(
                "inverters_production:    Inverter data not available for your Envoy device."
            )
        else:
            print(f"inverters_production:    {results[8]}")
        print(f"battery_storage:         {results[9]}")


if __name__ == "__main__":
    SECURE = ""

    parser = argparse.ArgumentParser(
        description="Retrieve energy information from the Enphase Envoy device."
    )
    parser.add_argument(
        "-u", "--user", dest="enlighten_user", help="Enlighten Username"
    )
    parser.add_argument(
        "-p", "--pass", dest="enlighten_pass", help="Enlighten Password"
    )
    parser.add_argument(
        "-c",
        "--comissioned",
        dest="commissioned",
        help="Commissioned Envoy (True/False)",
    )
    parser.add_argument(
        "-i",
        "--siteid",
        dest="enlighten_site_id",
        help="Enlighten Site ID. Only used when Commissioned=True.",
    )
    parser.add_argument(
        "-s",
        "--serialnum",
        dest="enlighten_serial_num",
        help="Enlighten Envoy Serial Numbewr. Only used when Commissioned=True.",
    )
    args = parser.parse_args()

    if (
        args.enlighten_user is not None
        and args.enlighten_pass is not None
        and args.commissioned is not None
    ):
        SECURE = "s"

    HOST = input(
        "Enter the Envoy IP address or host name, "
        + "or press enter to use 'envoy' as default: "
    )

    TOKEN = input(
        "Enter the Envoy token: "
    )
    
    USERNAME = input(
        "Enter the Username for Inverter data authentication, "
        + "or press enter to use 'envoy' as default: "
    )

    PASSWORD = input(
        "Enter the Password for Inverter data authentication, "
        + "or press enter to use the default password: "
    )

    if HOST == "":
        HOST = "envoy"

    if USERNAME == "":
        USERNAME = "envoy"
    
    if TOKEN:
        TESTREADER = EnvoyReader(
            HOST,
            USERNAME,
            inverters=True,
            enlighten_user=args.enlighten_user,
            enlighten_pass=args.enlighten_pass,
            commissioned=args.commissioned,
            enlighten_site_id=args.enlighten_site_id,
            enlighten_serial_num=args.enlighten_serial_num,
            https_flag=SECURE,
            token=TOKEN
        )
    elif PASSWORD == "":
        TESTREADER = EnvoyReader(
            HOST,
            USERNAME,
            inverters=True,
            enlighten_user=args.enlighten_user,
            enlighten_pass=args.enlighten_pass,
            commissioned=args.commissioned,
            enlighten_site_id=args.enlighten_site_id,
            enlighten_serial_num=args.enlighten_serial_num,
            https_flag=SECURE,
        )
    else:
        TESTREADER = EnvoyReader(
            HOST,
            USERNAME,
            PASSWORD,
            inverters=True,
            enlighten_user=args.enlighten_user,
            enlighten_pass=args.enlighten_pass,
            commissioned=args.commissioned,
            enlighten_site_id=args.enlighten_site_id,
            enlighten_serial_num=args.enlighten_serial_num,
            https_flag=SECURE,
        )

    TESTREADER.run_in_console()
