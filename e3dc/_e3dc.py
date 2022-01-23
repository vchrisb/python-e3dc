#!/usr/bin/env python
# Python class to connect to an E3/DC system through the internet portal
#
# Copyright 2017 Francesco Santini <francesco.santini@gmail.com>
# Licensed under a MIT license. See LICENSE for details

import datetime
import hashlib
import json
import time
import uuid

import dateutil.parser
import requests

from ._e3dc_rscp_local import (
    E3DC_RSCP_local,
    RSCPAuthenticationError,
    RSCPNotAvailableError,
)
from ._e3dc_rscp_web import E3DC_RSCP_web
from ._rscpLib import rscpFindTag

REMOTE_ADDRESS = "https://s10.e3dc.com/s10/phpcmd/cmd.php"
REQUEST_INTERVAL_SEC = 10  # minimum interval between requests
REQUEST_INTERVAL_SEC_LOCAL = 1  # minimum interval between requests


class AuthenticationError(Exception):
    pass


class NotAvailableError(Exception):
    pass


class PollError(Exception):
    pass


class SendError(Exception):
    pass


class E3DC:
    """A class describing an E3DC system, used to poll the status from the portal"""

    CONNECT_LOCAL = 1
    CONNECT_WEB = 2

    DAY_MONDAY = 0
    DAY_TUESDAY = 1
    DAY_WEDNESDAY = 2
    DAY_THURSDAY = 3
    DAY_FRIDAY = 4
    DAY_SATURDAY = 5
    DAY_SUNDAY = 6

    IDLE_TYPE = {"idleCharge": 0, "idleDischarge": 1}

    def __init__(self, connectType, **kwargs):
        """Constructor of a E3DC object (does not connect)

        Args:
            connectType: CONNECT_LOCAL: use local rscp connection
                Named args for CONNECT_LOCAL:
                username (string): username
                password (string): password (plain text)
                ipAddress (string): IP address of the E3DC system
                key (string): encryption key as set in the E3DC settings

            connectType: CONNECT_WEB: use web connection
                Named args for CONNECT_WEB:
                username (string): username
                password (string): password (plain text or md5 hash)
                serialNumber (string): the serial number of the system to monitor
                isPasswordMd5 (boolean, optional): indicates whether the password is already md5 digest (recommended, default = True)
        """

        self.connectType = connectType
        self.username = kwargs["username"]
        self.serialNumber = None
        self.serialNumberPrefix = None

        self.jar = None
        self.guid = "GUID-" + str(uuid.uuid1())
        self.lastRequestTime = -1
        self.lastRequest = None
        self.connected = False

        # static values
        self.deratePercent = None
        self.deratePower = None
        self.installedPeakPower = None
        self.installedBatteryCapacity = None
        self.externalSourceAvailable = None
        self.macAddress = None
        self.model = None
        self.maxAcPower = None
        self.maxBatChargePower = None
        self.maxBatDischargePower = None
        self.startDischargeDefault = None
        self.pmIndex = None
        self.pmIndexExt = None

        if connectType == self.CONNECT_LOCAL:
            self.ip = kwargs["ipAddress"]
            self.key = kwargs["key"]
            self.password = kwargs["password"]
            self.rscp = E3DC_RSCP_local(self.username, self.password, self.ip, self.key)
            self.poll = self.poll_rscp
        else:
            self._set_serial(kwargs["serialNumber"])
            if "isPasswordMd5" in kwargs:
                if kwargs["isPasswordMd5"]:
                    self.password = kwargs["password"]
                else:
                    self.password = hashlib.md5(
                        kwargs["password"].encode("utf-8")
                    ).hexdigest()
            self.rscp = E3DC_RSCP_web(
                self.username,
                self.password,
                "{}{}".format(self.serialNumberPrefix, self.serialNumber),
            )
            self.poll = self.poll_ajax

        self.get_system_info_static(keepAlive=True)

    def _set_serial(self, serial):
        if serial[0].isdigit():
            self.serialNumber = serial
        else:
            self.serialNumber = serial[4:]
            self.serialNumberPrefix = serial[:4]
        if self.serialNumber.startswith("4"):
            self.model = "S10E"
            self.pmIndex = 0
            self.pmIndexExt = 1
            if not self.serialNumberPrefix:
                self.serialNumberPrefix = "S10-"
        elif self.serialNumber.startswith("5"):
            self.model = "S10mini"
            self.pmIndex = 6
            self.pmIndexExt = 1
            if not self.serialNumberPrefix:
                self.serialNumberPrefix = "S10-"
        elif self.serialNumber.startswith("6"):
            self.model = "Quattroporte"
            self.pmIndex = 6
            self.pmIndexExt = 1
            if not self.serialNumberPrefix:
                self.serialNumberPrefix = "Q10-"
        elif self.serialNumber.startswith("7"):
            self.model = "Pro"
            self.pmIndex = 6
            self.pmIndexExt = 1
            if not self.serialNumberPrefix:
                self.serialNumberPrefix = "P10-"
        else:
            self.model = "NA"
            self.pmIndex = 0
            self.pmIndexExt = 1

    def connect_local(self):
        pass

    def connect_web(self):
        """Connects to the E3DC portal and opens a session

        Raises:
            e3dc.AuthenticationError: login error
        """
        # login request
        loginPayload = {
            "DO": "LOGIN",
            "USERNAME": self.username,
            "PASSWD": self.password,
        }
        headers = {"Window-Id": self.guid}

        try:
            r = requests.post(REMOTE_ADDRESS, data=loginPayload, headers=headers)
            jsonResponse = r.json()
        except:
            raise AuthenticationError("Error communicating with server")
        if jsonResponse["ERRNO"] != 0:
            raise AuthenticationError("Login error")

        # get cookies
        self.jar = r.cookies

        # set the proper device
        deviceSelectPayload = {
            "DO": "GETCONTENT",
            "MODID": "IDOVERVIEWUNITMAIN",
            "ARG0": self.serialNumber,
            "TOS": -7200,
        }

        try:
            r = requests.post(
                REMOTE_ADDRESS,
                data=deviceSelectPayload,
                cookies=self.jar,
                headers=headers,
            )
            jsonResponse = r.json()
        except:
            raise AuthenticationError("Error communicating with server")
        if jsonResponse["ERRNO"] != 0:
            raise AuthenticationError("Error selecting device")
        self.connected = True

    def poll_ajax_raw(self):
        """Polls the portal for the current status

        Returns:
            Dictionary containing the status information in raw format as returned by the portal

        Raises:
            e3dc.PollError in case of problems polling
        """

        if not self.connected:
            self.connect_web()

        pollPayload = {"DO": "LIVEUNITDATA"}
        pollHeaders = {
            "Pragma": "no-cache",
            "Cache-Control": "no-store",
            "Window-Id": self.guid,
        }

        try:
            r = requests.post(
                REMOTE_ADDRESS, data=pollPayload, cookies=self.jar, headers=pollHeaders
            )
            jsonResponse = r.json()
        except:
            self.connected = False
            raise PollError("Error communicating with server")

        if jsonResponse["ERRNO"] != 0:
            raise PollError("Error polling: %d" % (jsonResponse["ERRNO"]))

        return json.loads(jsonResponse["CONTENT"])

    def poll_ajax(self, **kwargs):
        """Polls the portal for the current status and returns a digest

        Returns:
            Dictionary containing the condensed status information structured as follows:
                {
                    'time': datetime object containing the timestamp
                    'sysStatus': string containing the system status code
                    'stateOfCharge': battery charge status in %
                    'consumption': { consumption values: positive means exiting the system
                        'battery': power entering battery (positive: charging, negative: discharging)
                        'house': house consumption
                        'wallbox': wallbox consumption
                    },
                    'production': { production values: positive means entering the system
                        'solar' : production from solar in W
                        'add' : additional external power in W
                        'grid' : absorption from grid in W
                        }
                }

        Raises:
            e3dc.PollError in case of problems polling
        """
        if (
            self.lastRequest is not None
            and (time.time() - self.lastRequestTime) < REQUEST_INTERVAL_SEC
        ):
            return self.lastRequest

        raw = self.poll_ajax_raw()
        strPmIndex = str(self.pmIndexExt)
        outObj = {
            "time": dateutil.parser.parse(raw["time"]).replace(
                tzinfo=datetime.timezone.utc
            ),
            "sysStatus": raw["SYSSTATUS"],
            "stateOfCharge": int(raw["SOC"]),
            "production": {
                "solar": int(raw["POWER_PV_S1"])
                + int(raw["POWER_PV_S2"])
                + int(raw["POWER_PV_S3"]),
                "add": -(
                    int(raw["PM" + strPmIndex + "_L1"])
                    + int(raw["PM" + strPmIndex + "_L2"])
                    + int(raw["PM" + strPmIndex + "_L3"])
                ),
                "grid": int(raw["POWER_LM_L1"])
                + int(raw["POWER_LM_L2"])
                + int(raw["POWER_LM_L3"]),
            },
            "consumption": {
                "battery": int(raw["POWER_BAT"]),
                "house": int(raw["POWER_C_L1"])
                + int(raw["POWER_C_L2"])
                + int(raw["POWER_C_L3"]),
                "wallbox": int(raw["POWER_WALLBOX"]),
            },
        }

        self.lastRequest = outObj
        self.lastRequestTime = time.time()

        return outObj

    def poll_rscp(self, keepAlive=False):
        """Polls via rscp protocol locally

        Returns:
            Dictionary containing the condensed status information structured as follows:
                {
                    'autarky': autarky in %
                    'consumption': { consumption values: positive means exiting the system
                        'battery': power entering battery (positive: charging, negative: discharging)
                        'house': house consumption
                        'wallbox': wallbox consumption
                    }
                    'production': { production values: positive means entering the system
                        'solar' : production from solar in W
                        'add' : additional external power in W
                        'grid' : absorption from grid in W
                    }
                    'stateOfCharge': battery charge status in %
                    'selfConsumption': self consumed power in %
                    'time': datetime object containing the timestamp
                }
        """
        if (
            self.lastRequest is not None
            and (time.time() - self.lastRequestTime) < REQUEST_INTERVAL_SEC_LOCAL
        ):
            return self.lastRequest

        ts = self.sendRequest(("INFO_REQ_UTC_TIME", "None", None), keepAlive=True)[2]
        soc = self.sendRequest(("EMS_REQ_BAT_SOC", "None", None), keepAlive=True)[2]
        solar = self.sendRequest(("EMS_REQ_POWER_PV", "None", None), keepAlive=True)[2]
        add = self.sendRequest(("EMS_REQ_POWER_ADD", "None", None), keepAlive=True)[2]
        bat = self.sendRequest(("EMS_REQ_POWER_BAT", "None", None), keepAlive=True)[2]
        home = self.sendRequest(("EMS_REQ_POWER_HOME", "None", None), keepAlive=True)[2]
        grid = self.sendRequest(("EMS_REQ_POWER_GRID", "None", None), keepAlive=True)[2]
        wb = self.sendRequest(("EMS_REQ_POWER_WB_ALL", "None", None), keepAlive=True)[2]

        sc = round(
            self.sendRequest(
                ("EMS_REQ_SELF_CONSUMPTION", "None", None), keepAlive=True
            )[2],
            2,
        )

        # last call, use keepAlive value
        autarky = round(
            self.sendRequest(("EMS_REQ_AUTARKY", "None", None), keepAlive=keepAlive)[2],
            2,
        )

        outObj = {
            "autarky": autarky,
            "consumption": {"battery": bat, "house": home, "wallbox": wb},
            "production": {"solar": solar, "add": -add, "grid": grid},
            "selfConsumption": sc,
            "stateOfCharge": soc,
            "time": datetime.datetime.utcfromtimestamp(ts).replace(
                tzinfo=datetime.timezone.utc
            ),
        }

        self.lastRequest = outObj
        self.lastRequestTime = time.time()
        return outObj

    def poll_switches(self, keepAlive=False):
        """
        This function uses the RSCP interface to poll the switch status
        if keepAlive is False, the connection is closed afterwards
        """

        if not self.rscp.isConnected():
            self.rscp.connect()

        switchDesc = self.sendRequest(
            ("HA_REQ_DATAPOINT_LIST", "None", None), keepAlive=True
        )
        switchStatus = self.sendRequest(
            ("HA_REQ_ACTUATOR_STATES", "None", None), keepAlive=keepAlive
        )

        descList = switchDesc[2]  # get the payload of the container
        statusList = switchStatus[2]

        # print switchStatus

        switchList = []

        for switch in range(len(descList)):
            switchID = rscpFindTag(descList[switch], "HA_DATAPOINT_INDEX")[2]
            switchType = rscpFindTag(descList[switch], "HA_DATAPOINT_TYPE")[2]
            switchName = rscpFindTag(descList[switch], "HA_DATAPOINT_NAME")[2]
            switchStatus = rscpFindTag(statusList[switch], "HA_DATAPOINT_STATE")[2]
            switchList.append(
                {
                    "id": switchID,
                    "type": switchType,
                    "name": switchName,
                    "status": switchStatus,
                }
            )

        return switchList

    def set_switch_onoff(self, switchID, value, keepAlive=False):
        """
        This function uses the RSCP interface to turn a switch on or off
        The switchID is as returned by poll_switches
        """

        cmd = "on" if value else "off"

        result = self.sendRequest(
            (
                "HA_REQ_COMMAND_ACTUATOR",
                "Container",
                [
                    ("HA_DATAPOINT_INDEX", "Uint16", switchID),
                    ("HA_REQ_COMMAND", "CString", cmd),
                ],
            ),
            keepAlive=keepAlive,
        )

        if result[0] == "HA_COMMAND_ACTUATOR" and result[2]:
            return True
        else:
            return False  # operation did not succeed

    def sendRequest(self, request, retries=3, keepAlive=False):
        """
        This function uses the RSCP interface to make an request
        Does make retries in case of exceptions like Socket.Error

        Returns:
            An object with the received data
        Raises:
            e3dc.AuthenticationError: login error
            e3dc.SendError: if retries are reached
        """
        retry = 0
        while True:
            try:
                if not self.rscp.isConnected():
                    self.rscp.connect()
                result = self.rscp.sendRequest(request)
                break
            except RSCPAuthenticationError:
                raise AuthenticationError()
            except RSCPNotAvailableError:
                raise NotAvailableError()
            except Exception:
                retry += 1
                if retry > retries:
                    raise SendError("Max retries reached")

        if not keepAlive:
            self.rscp.disconnect()

        return result

    def get_idle_periods(self, keepAlive=False):
        """poll via rscp protocol to get idle periods

        Returns:
            Dictionary containing the idle periods:
                {
                    'idleCharge': list of the idle charge times
                    [
                        {
                            'day': the week day from 0 to 6
                            'start': list of start time
                            [
                                int: hour from 0 to 23
                                int: minute from 0 to 59
                            ]
                            'end': list of end time
                            [
                                int: hour from 0 to 23
                                int: minute from 0 to 59
                            [
                            'active': boolean of state
                        }
                    ]
                    'idleDischarge': list of the idle discharge times
                    [
                        {
                            'day': the week day from 0 to 6
                            'start': list of start time
                            [
                                int: hour from 0 to 23
                                int: minute from 0 to 59
                            ]
                            'end': list of end time
                            [
                                int: hour from 0 to 23
                                int: minute from 0 to 59
                            [
                            'active': boolean of state
                        }
                    ]
                }
        """
        idlePeriodsRaw = self.sendRequest(
            ("EMS_REQ_GET_IDLE_PERIODS", "None", None), keepAlive=keepAlive
        )
        if idlePeriodsRaw[0] != "EMS_GET_IDLE_PERIODS":
            return None

        idlePeriods = {"idleCharge": [None] * 7, "idleDischarge": [None] * 7}

        # initialize
        for period in idlePeriodsRaw[2]:
            active = rscpFindTag(period, "EMS_IDLE_PERIOD_ACTIVE")[2]
            typ = rscpFindTag(period, "EMS_IDLE_PERIOD_TYPE")[2]
            day = rscpFindTag(period, "EMS_IDLE_PERIOD_DAY")[2]
            start = rscpFindTag(period, "EMS_IDLE_PERIOD_START")
            startHour = rscpFindTag(start, "EMS_IDLE_PERIOD_HOUR")[2]
            startMin = rscpFindTag(start, "EMS_IDLE_PERIOD_MINUTE")[2]
            end = rscpFindTag(period, "EMS_IDLE_PERIOD_END")
            endHour = rscpFindTag(end, "EMS_IDLE_PERIOD_HOUR")[2]
            endMin = rscpFindTag(end, "EMS_IDLE_PERIOD_MINUTE")[2]
            periodObj = {
                "day": day,
                "start": [startHour, startMin],
                "end": [endHour, endMin],
                "active": active,
            }

            if typ == self.IDLE_TYPE["idleCharge"]:
                idlePeriods["idleCharge"][day] = periodObj
            else:
                idlePeriods["idleDischarge"][day] = periodObj

        return idlePeriods

    def set_idle_periods(self, idlePeriods, keepAlive=False):
        """set via rscp protocol the idle periods

        Inputs:
            idlePeriods: Dictionary containing one or many idle periods
                {
                    'idleCharge': list of the idle charge times
                    [
                        {
                            'day': the week day from 0 to 6
                            'start': list of start time
                            [
                                int: hour from 0 to 23
                                int: minute from 0 to 59
                            ]
                            'end': list of end time
                            [
                                int: hour from 0 to 23
                                int: minute from 0 to 59
                            [
                            'active': boolean of state
                        }
                    ],
                    'idleDischarge': list of the idle discharge times
                    [
                        {
                            'day': the week day from 0 to 6
                            'start': list of start time
                            [
                                int: hour from 0 to 23
                                int: minute from 0 to 59
                            ]
                            'end': list of end time
                            [
                                int: hour from 0 to 23
                                int: minute from 0 to 59
                            [
                            'active': boolean of state
                        }
                    ]
                }
        Returns:
            True if success
            False if error
        """

        periodList = []

        if not isinstance(idlePeriods, dict):
            raise TypeError("object is not a dict")
        elif "idleCharge" not in idlePeriods and "idleDischarge" not in idlePeriods:
            raise ValueError("neither key idleCharge nor idleDischarge in object")

        for idle_type in ["idleCharge", "idleDischarge"]:
            if idle_type in idlePeriods:
                if isinstance(idlePeriods[idle_type], list):
                    for idlePeriod in idlePeriods[idle_type]:
                        if isinstance(idlePeriod, dict):
                            if "day" not in idlePeriod:
                                raise ValueError("day key in " + idle_type + " missing")
                            elif isinstance(idlePeriod["day"], bool):
                                raise TypeError("day in " + idle_type + " not a bool")
                            elif not (0 <= idlePeriod["day"] <= 6):
                                raise ValueError(
                                    "day in " + idle_type + " out of range"
                                )

                            if idlePeriod.keys() & ["active", "start", "end"]:
                                if "active" in idlePeriod:
                                    if isinstance(idlePeriod["active"], bool):
                                        idlePeriod["active"] = idlePeriod["active"]
                                    else:
                                        raise TypeError(
                                            "period "
                                            + str(idlePeriod["day"])
                                            + " in "
                                            + idle_type
                                            + " not a bool"
                                        )

                                for key in ["start", "end"]:
                                    if key in idlePeriod:
                                        if (
                                            isinstance(idlePeriod[key], list)
                                            and len(idlePeriod[key]) == 2
                                        ):
                                            for i in range(2):
                                                if isinstance(idlePeriod[key][i], int):
                                                    if idlePeriod[key][i] >= 0 and (
                                                        (
                                                            i == 0
                                                            and idlePeriod[key][i] < 24
                                                        )
                                                        or (
                                                            i == 1
                                                            and idlePeriod[key][i] < 60
                                                        )
                                                    ):
                                                        idlePeriod[key][i] = idlePeriod[
                                                            key
                                                        ][i]
                                                    else:
                                                        raise ValueError(
                                                            key
                                                            in " period "
                                                            + str(idlePeriod["day"])
                                                            + " in "
                                                            + idle_type
                                                            + " is not between 00:00 and 23:59"
                                                        )
                                if (
                                    idlePeriod["start"][0] * 60 + idlePeriod["start"][1]
                                ) < (idlePeriod["end"][0] * 60 + idlePeriod["end"][1]):
                                    periodList.append(
                                        (
                                            "EMS_IDLE_PERIOD",
                                            "Container",
                                            [
                                                (
                                                    "EMS_IDLE_PERIOD_TYPE",
                                                    "UChar8",
                                                    self.IDLE_TYPE[idle_type],
                                                ),
                                                (
                                                    "EMS_IDLE_PERIOD_DAY",
                                                    "UChar8",
                                                    idlePeriod["day"],
                                                ),
                                                (
                                                    "EMS_IDLE_PERIOD_ACTIVE",
                                                    "Bool",
                                                    idlePeriod["active"],
                                                ),
                                                (
                                                    "EMS_IDLE_PERIOD_START",
                                                    "Container",
                                                    [
                                                        (
                                                            "EMS_IDLE_PERIOD_HOUR",
                                                            "UChar8",
                                                            idlePeriod["start"][0],
                                                        ),
                                                        (
                                                            "EMS_IDLE_PERIOD_MINUTE",
                                                            "UChar8",
                                                            idlePeriod["start"][1],
                                                        ),
                                                    ],
                                                ),
                                                (
                                                    "EMS_IDLE_PERIOD_END",
                                                    "Container",
                                                    [
                                                        (
                                                            "EMS_IDLE_PERIOD_HOUR",
                                                            "UChar8",
                                                            idlePeriod["end"][0],
                                                        ),
                                                        (
                                                            "EMS_IDLE_PERIOD_MINUTE",
                                                            "UChar8",
                                                            idlePeriod["end"][1],
                                                        ),
                                                    ],
                                                ),
                                            ],
                                        )
                                    )
                                else:
                                    raise ValueError(
                                        "end time is smaller than start time in period "
                                        + str(idlePeriod["day"])
                                        + " in "
                                        + idle_type
                                        + " is not between 00:00 and 23:59"
                                    )

                        else:
                            raise TypeError("period in " + idle_type + " is not a dict")

                else:
                    raise TypeError(idle_type + " is not a dict")

        result = self.sendRequest(
            ("EMS_REQ_SET_IDLE_PERIODS", "Container", periodList), keepAlive=keepAlive
        )

        if result[0] != "EMS_SET_IDLE_PERIODS" or result[2] != 1:
            return False
        return True

    def get_db_data(
        self, startDate: datetime.date = None, timespan: str = "DAY", keepAlive=False
    ):
        """
        Reads DB data and summed up values for the given timespan via rscp protocol locally
        All parameters are optional, but if none is given, the db data for today is retrieved
        Possible values for timespan are 'YEAR', 'MONTH' or 'DAY'

        Returns:
            Dictionary containing the stored db information structured as follows:

            {
            'bat_power_in': power entering battery, charging
            'bat_power_out': power leavinb battery, discharging
            'solarProduction': power production
            'grid_power_in': power taken from the grid
            'grid_power_out': power into the grid
            'consumption':  self consumed power
            'stateOfCharge': battery charge level in %
            'consumed_production':  power directly consumed in %
            'autarky':  autarky in the period in %
            }
        """

        span: int = 0
        if startDate is None:
            startDate = datetime.date.today()
        requestDate: int = int(time.mktime(startDate.timetuple()))

        if "YEAR" == timespan:
            spanDate = startDate.replace(year=startDate.year + 1)
            span = int(time.mktime(spanDate.timetuple()) - requestDate)
        if "MONTH" == timespan:
            if 12 == startDate.month:
                spanDate = startDate.replace(month=1, year=startDate.year + 1)
            else:
                spanDate = startDate.replace(month=startDate.month + 1)
            span = int(time.mktime(spanDate.timetuple()) - requestDate)
        if "DAY" == timespan:
            span = 24 * 60 * 60

        if span == 0:
            return None

        response = self.sendRequest(
            (
                "DB_REQ_HISTORY_DATA_DAY",
                "Container",
                [
                    ("DB_REQ_HISTORY_TIME_START", "Uint64", requestDate),
                    ("DB_REQ_HISTORY_TIME_INTERVAL", "Uint64", span),
                    ("DB_REQ_HISTORY_TIME_SPAN", "Uint64", span),
                ],
            ),
            keepAlive=keepAlive,
        )

        outObj = {
            "bat_power_in": rscpFindTag(response[2][0], "DB_BAT_POWER_IN")[2],
            "bat_power_out": rscpFindTag(response[2][0], "DB_BAT_POWER_OUT")[2],
            "solarProduction": rscpFindTag(response[2][0], "DB_DC_POWER")[2],
            "grid_power_in": rscpFindTag(response[2][0], "DB_GRID_POWER_IN")[2],
            "grid_power_out": rscpFindTag(response[2][0], "DB_GRID_POWER_OUT")[2],
            "consumption": rscpFindTag(response[2][0], "DB_CONSUMPTION")[2],
            "stateOfCharge": rscpFindTag(response[2][0], "DB_BAT_CHARGE_LEVEL")[2],
            "consumed_production": rscpFindTag(
                response[2][0], "DB_CONSUMED_PRODUCTION"
            )[2],
            "autarky": rscpFindTag(response[2][0], "DB_AUTARKY")[2],
        }
        return outObj

    def get_system_info_static(self, keepAlive=False):
        """Polls the static system info via rscp protocol locally"""

        self.deratePercent = round(
            self.sendRequest(
                ("EMS_REQ_DERATE_AT_PERCENT_VALUE", "None", None), keepAlive=True
            )[2]
            * 100
        )
        self.deratePower = self.sendRequest(
            ("EMS_REQ_DERATE_AT_POWER_VALUE", "None", None), keepAlive=True
        )[2]
        self.installedPeakPower = self.sendRequest(
            ("EMS_REQ_INSTALLED_PEAK_POWER", "None", None), keepAlive=True
        )[2]
        self.externalSourceAvailable = self.sendRequest(
            ("EMS_REQ_EXT_SRC_AVAILABLE", "None", None), keepAlive=True
        )[2]
        self.macAddress = self.sendRequest(
            ("INFO_REQ_MAC_ADDRESS", "None", None), keepAlive=True
        )[2]
        if (
            not self.serialNumber
        ):  # do not send this for a web connection because it screws up the handshake!
            self._set_serial(
                self.sendRequest(
                    ("INFO_REQ_SERIAL_NUMBER", "None", None), keepAlive=True
                )[2]
            )

        sys_specs = self.sendRequest(
            ("EMS_REQ_GET_SYS_SPECS", "None", None), keepAlive=keepAlive
        )[2]
        for item in sys_specs:
            if rscpFindTag(item, "EMS_SYS_SPEC_NAME")[2] == "installedBatteryCapacity":
                self.installedBatteryCapacity = rscpFindTag(
                    item, "EMS_SYS_SPEC_VALUE_INT"
                )[2]
            elif rscpFindTag(item, "EMS_SYS_SPEC_NAME")[2] == "maxAcPower":
                self.maxAcPower = rscpFindTag(item, "EMS_SYS_SPEC_VALUE_INT")[2]
            elif rscpFindTag(item, "EMS_SYS_SPEC_NAME")[2] == "maxBatChargePower":
                self.maxBatChargePower = rscpFindTag(item, "EMS_SYS_SPEC_VALUE_INT")[2]
            elif rscpFindTag(item, "EMS_SYS_SPEC_NAME")[2] == "maxBatDischargPower":
                self.maxBatDischargePower = rscpFindTag(item, "EMS_SYS_SPEC_VALUE_INT")[
                    2
                ]

        # EMS_REQ_SPECIFICATION_VALUES

        return True

    def get_system_info(self, keepAlive=False):
        """Polls the system info via rscp protocol locally

        Returns:
            Dictionary containing the system info structured as follows:
                {
                    'deratePercent': % of installed peak power the feed in will be derated
                    'deratePower': W at which the feed in will be derated
                    'installedBatteryCapacity': installed Battery Capacity in W
                    'installedPeakPower': installed peak power in W
                    'externalSourceAvailable': wether an additional power meter is installed
                    'maxAcPower': max AC power
                    'macAddress': the mac address
                    'maxBatChargePower': max Battery charge power
                    'maxBatDischargePower': max Battery discharge power
                    'model': model connected to
                    'release': release version
                    'serial': serial number of the system
                }
        """

        # use keepAlive setting for last request
        sw = self.sendRequest(
            ("INFO_REQ_SW_RELEASE", "None", None), keepAlive=keepAlive
        )[2]

        # EMS_EMERGENCY_POWER_STATUS

        outObj = {
            "deratePercent": self.deratePercent,
            "deratePower": self.deratePower,
            "installedBatteryCapacity": self.installedBatteryCapacity,
            "installedPeakPower": self.installedPeakPower,
            "externalSourceAvailable": self.externalSourceAvailable,
            "maxAcPower": self.maxAcPower,
            "macAddress": self.macAddress,
            "maxBatChargePower": self.maxBatChargePower,
            "maxBatDischargePower": self.maxBatDischargePower,
            "model": self.model,
            "release": sw,
            "serial": self.serialNumber,
        }
        return outObj

    def get_system_status(self, keepAlive=False):
        """Polls the system status via rscp protocol locally

        Returns:
            Dictionary containing the system status structured as follows:
                {
                    'dcdcAlive': dcdc alive
                    'powerMeterAlive': power meter alive
                    'batteryModuleAlive': battery module alive
                    'pvModuleAlive': pv module alive
                    'pvInverterInited': pv inverter inited
                    'serverConnectionAlive': server connection alive
                    'pvDerated':  pv derated due to deratePower limit reached
                    'emsAlive': emd alive
                    'acModeBlocked': ad mode blocked
                    'sysConfChecked': sys conf checked
                    'emergencyPowerStarted': emergency power started
                    'emergencyPowerOverride': emergency power override
                    'wallBoxAlive': wall box alive
                    'powerSaveEnabled': power save enabled
                    'chargeIdlePeriodActive': charge idle period active
                    'dischargeIdlePeriodActive': discharge idle period active
                    'waitForWeatherBreakthrough': wait for weather breakthrouhgh
                    'rescueBatteryEnabled': rescue battery enabled
                    'emergencyReserveReached': emergencey reserve reached
                    'socSyncRequested': soc sync requested
                }
        """

        # use keepAlive setting for last request
        sw = self.sendRequest(
            ("EMS_REQ_SYS_STATUS", "None", None), keepAlive=keepAlive
        )[2]
        SystemStatusBools = [bool(int(i)) for i in reversed(list(f"{sw:022b}"))]

        outObj = {
            "dcdcAlive": 0,
            "powerMeterAlive": 1,
            "batteryModuleAlive": 2,
            "pvModuleAlive": 3,
            "pvInverterInited": 4,
            "serverConnectionAlive": 5,
            "pvDerated": 6,
            "emsAlive": 7,
            # 'acCouplingMode:2;              // 8-9
            "acModeBlocked": 10,
            "sysConfChecked": 11,
            "emergencyPowerStarted": 12,
            "emergencyPowerOverride": 13,
            "wallBoxAlive": 14,
            "powerSaveEnabled": 15,
            "chargeIdlePeriodActive": 16,
            "dischargeIdlePeriodActive": 17,
            "waitForWeatherBreakthrough": 18,  # this status bit shows if weather regulated charge is active and the system is waiting for the sun power breakthrough. (PV power > derating power)
            "rescueBatteryEnabled": 19,
            "emergencyReserveReached": 20,
            "socSyncRequested": 21,
        }
        outObj = {k: SystemStatusBools[v] for k, v in outObj.items()}
        return outObj

    def get_battery_data(self, batIndex=0, dcb=None, keepAlive=False):
        """Polls the baterry data via rscp protocol locally

        Returns:
            Dictionary containing the battery data structured as follows:
                {
                    'batIndex': battery index
                    'chargeCycles': charge cycles
                    'current': current
                    'designCapacity': designed capacity
                    'deviceConnected': boolean if battery connected
                    'deviceInService': boolean if battery in service
                    'deviceName': device name
                    'deviceWorking': boolean if battery working
                    'eodVoltage': end of discharge voltage
                    'errorCode': error code
                    'maxBatVoltage': maximum battery voltage
                    'maxChargeCurrent': maximum charge current
                    'maxDcbCellTemp': maximum Dcb cell temp
                    'maxDischargeCurrent': maximum discharge current
                    'moduleVoltage': module voltage
                    'rsoc': state of charge
                    'statusCode': status code
                    'terminalVoltage': terminal voltage
                    'usuableCapacity': usuable capacity
                    'usuableRemainingCapacity': usuable remaining capacity
                }
        """

        req = self.sendRequest(
            (
                "BAT_REQ_DATA",
                "Container",
                [
                    ("BAT_INDEX", "Uint16", batIndex),
                    ("BAT_REQ_ASOC", "None", None),
                    ("BAT_REQ_CHARGE_CYCLES", "None", None),
                    ("BAT_REQ_CURRENT", "None", None),
                    ("BAT_REQ_DCB_COUNT", "None", None),
                    ("BAT_REQ_DESIGN_CAPACITY", "None", None),
                    ("BAT_REQ_DEVICE_NAME", "None", None),
                    ("BAT_REQ_DEVICE_STATE", "None", None),
                    ("BAT_REQ_EOD_VOLTAGE", "None", None),
                    ("BAT_REQ_ERROR_CODE", "None", None),
                    ("BAT_REQ_FCC", "None", None),
                    ("BAT_REQ_MAX_BAT_VOLTAGE", "None", None),
                    ("BAT_REQ_MAX_CHARGE_CURRENT", "None", None),
                    ("BAT_REQ_MAX_DISCHARGE_CURRENT", "None", None),
                    ("BAT_REQ_MAX_DCB_CELL_TEMPERATURE", "None", None),
                    ("BAT_REQ_MIN_DCB_CELL_TEMPERATURE", "None", None),
                    ("BAT_REQ_INTERNALS", "None", None),
                    ("BAT_REQ_MODULE_VOLTAGE", "None", None),
                    ("BAT_REQ_RC", "None", None),
                    ("BAT_REQ_READY_FOR_SHUTDOWN", "None", None),
                    ("BAT_REQ_RSOC", "None", None),
                    ("BAT_REQ_RSOC_REAL", "None", None),
                    ("BAT_REQ_STATUS_CODE", "None", None),
                    ("BAT_REQ_TERMINAL_VOLTAGE", "None", None),
                    ("BAT_REQ_TOTAL_USE_TIME", "None", None),
                    ("BAT_REQ_TOTAL_DISCHARGE_TIME", "None", None),
                    ("BAT_REQ_TRAINING_MODE", "None", None),
                    ("BAT_REQ_USABLE_CAPACITY", "None", None),
                    ("BAT_REQ_USABLE_REMAINING_CAPACITY", "None", None),
                ],
            ),
            keepAlive=True,
        )

        print(req)

        dcbCount = rscpFindTag(req, "BAT_DCB_COUNT")[2]
        deviceStateContainer = rscpFindTag(req, "BAT_DEVICE_STATE")

        outObj = {
            "asoc": rscpFindTag(req, "BAT_ASOC")[2],
            "batIndex": batIndex,
            "chargeCycles": rscpFindTag(req, "BAT_CHARGE_CYCLES")[2],
            "current": round(rscpFindTag(req, "BAT_CURRENT")[2], 2),
            "dcbCount": dcbCount,
            "dcbs": {},
            "designCapacity": round(rscpFindTag(req, "BAT_DESIGN_CAPACITY")[2], 2),
            "deviceConnected": rscpFindTag(
                deviceStateContainer, "BAT_DEVICE_CONNECTED"
            )[2],
            "deviceInService": rscpFindTag(
                deviceStateContainer, "BAT_DEVICE_IN_SERVICE"
            )[2],
            "deviceName": rscpFindTag(req, "BAT_DEVICE_NAME")[2],
            "deviceWorking": rscpFindTag(deviceStateContainer, "BAT_DEVICE_WORKING")[2],
            "eodVoltage": round(rscpFindTag(req, "BAT_EOD_VOLTAGE")[2], 2),
            "errorCode": rscpFindTag(req, "BAT_ERROR_CODE")[2],
            "fcc": rscpFindTag(req, "BAT_FCC")[2],
            "maxBatVoltage": round(rscpFindTag(req, "BAT_MAX_BAT_VOLTAGE")[2], 2),
            "maxChargeCurrent": round(rscpFindTag(req, "BAT_MAX_CHARGE_CURRENT")[2], 2),
            "maxDischargeCurrent": round(
                rscpFindTag(req, "BAT_MAX_DISCHARGE_CURRENT")[2], 2
            ),
            "maxDcbCellTemp": round(
                rscpFindTag(req, "BAT_MAX_DCB_CELL_TEMPERATURE")[2], 2
            ),
            "measuredResistance": round(
                rscpFindTag(req, "BAT_MEASURED_RESISTANCE")[2], 4
            ),
            "measuredResistanceRun": round(
                rscpFindTag(req, "BAT_RUN_MEASURED_RESISTANCE")[2], 4
            ),
            "minDcbCellTemp": round(
                rscpFindTag(req, "BAT_MIN_DCB_CELL_TEMPERATURE")[2], 2
            ),
            "moduleVoltage": round(rscpFindTag(req, "BAT_MODULE_VOLTAGE")[2], 2),
            "rc": round(rscpFindTag(req, "BAT_RC")[2], 2),
            "readyForShutdown": round(rscpFindTag(req, "BAT_READY_FOR_SHUTDOWN")[2], 2),
            "rsoc": round(rscpFindTag(req, "BAT_RSOC")[2], 2),
            "rsocReal": round(rscpFindTag(req, "BAT_RSOC_REAL")[2], 2),
            "statusCode": rscpFindTag(req, "BAT_STATUS_CODE")[2],
            "terminalVoltage": round(rscpFindTag(req, "BAT_TERMINAL_VOLTAGE")[2], 2),
            "totalUseTime": rscpFindTag(req, "BAT_TOTAL_USE_TIME")[2],
            "totalDischargeTime": rscpFindTag(req, "BAT_TOTAL_DISCHARGE_TIME")[2],
            "trainingMode": rscpFindTag(req, "BAT_TRAINING_MODE")[2],
            "usuableCapacity": round(rscpFindTag(req, "BAT_USABLE_CAPACITY")[2], 2),
            "usuableRemainingCapacity": round(
                rscpFindTag(req, "BAT_USABLE_REMAINING_CAPACITY")[2], 2
            ),
        }

        if dcb is None:
            dcbs = range(0, dcbCount)
        elif isinstance(dcbIndex, list):
            dcbs = dcb
        else:
            dcbs = [dcb]

        for dcb in dcbs:
            req = self.sendRequest(
                (
                    "BAT_REQ_DATA",
                    "Container",
                    [
                        ("BAT_INDEX", "Uint16", batIndex),
                        ("BAT_REQ_DCB_ALL_CELL_TEMPERATURES", "Uint16", dcb),
                        ("BAT_REQ_DCB_ALL_CELL_VOLTAGES", "Uint16", dcb),
                        ("BAT_REQ_DCB_INFO", "Uint16", dcb),
                    ],
                ),
                keepAlive=keepAlive if dcb == dcbs else True,
            )

            info = rscpFindTag(req, "BAT_DCB_INFO")

            temperatures_raw = rscpFindTag(
                rscpFindTag(req, "BAT_DCB_ALL_CELL_TEMPERATURES"), "BAT_DATA"
            )[2]
            temperatures = []
            sensorCount = rscpFindTag(info, "BAT_DCB_NR_SENSOR")[2]
            for sensor in range(0, sensorCount):
                temperatures.append(round(temperatures_raw[sensor][2], 2))

            voltages_raw = rscpFindTag(
                rscpFindTag(req, "BAT_DCB_ALL_CELL_VOLTAGES"), "BAT_DATA"
            )[2]
            voltages = []
            seriesCellCount = rscpFindTag(info, "BAT_DCB_NR_SERIES_CELL")[2]
            for cell in range(0, seriesCellCount):
                voltages.append(round(voltages_raw[cell][2], 2))

            dcbobj = {
                "current": rscpFindTag(info, "BAT_DCB_CURRENT")[2],
                "currentAvg30s": rscpFindTag(info, "BAT_DCB_CURRENT_AVG_30S")[2],
                "cycleCount": rscpFindTag(info, "BAT_DCB_CYCLE_COUNT")[2],
                "designCapacity": rscpFindTag(info, "BAT_DCB_DESIGN_CAPACITY")[2],
                "designVoltage": rscpFindTag(info, "BAT_DCB_DESIGN_VOLTAGE")[2],
                "deviceName": rscpFindTag(info, "BAT_DCB_DEVICE_NAME")[2],
                "endOfDischarge": rscpFindTag(info, "BAT_DCB_END_OF_DISCHARGE")[2],
                "error": rscpFindTag(info, "BAT_DCB_ERROR")[2],
                "fullChargeCapacity": rscpFindTag(info, "BAT_DCB_FULL_CHARGE_CAPACITY")[
                    2
                ],
                "fwVersion": rscpFindTag(info, "BAT_DCB_FW_VERSION")[2],
                "manufactureDate": rscpFindTag(info, "BAT_DCB_MANUFACTURE_DATE")[2],
                "manufactureName": rscpFindTag(info, "BAT_DCB_MANUFACTURE_NAME")[2],
                "maxChargeCurrent": rscpFindTag(info, "BAT_DCB_MAX_CHARGE_CURRENT")[2],
                "maxChargeTemperature": rscpFindTag(
                    info, "BAT_DCB_CHARGE_HIGH_TEMPERATURE"
                )[2],
                "maxChargeVoltage": rscpFindTag(info, "BAT_DCB_MAX_CHARGE_VOLTAGE")[2],
                "maxDischargeCurrent": rscpFindTag(
                    info, "BAT_DCB_MAX_DISCHARGE_CURRENT"
                )[2],
                "minChargeTemperature": rscpFindTag(
                    info, "BAT_DCB_CHARGE_LOW_TEMPERATURE"
                )[2],
                "parallelCellCount": rscpFindTag(info, "BAT_DCB_NR_PARALLEL_CELL")[2],
                "sensorCount": sensorCount,
                "seriesCellCount": seriesCellCount,
                "pcbVersion": rscpFindTag(info, "BAT_DCB_PCB_VERSION")[2],
                "protocolVersion": rscpFindTag(info, "BAT_DCB_PROTOCOL_VERSION")[2],
                "remainingCapacity": rscpFindTag(info, "BAT_DCB_REMAINING_CAPACITY")[2],
                "serialCode": rscpFindTag(info, "BAT_DCB_SERIALCODE")[2],
                "serialNo": rscpFindTag(info, "BAT_DCB_SERIALNO")[2],
                "soc": rscpFindTag(info, "BAT_DCB_SOC")[2],
                "soh": rscpFindTag(info, "BAT_DCB_SOH")[2],
                "status": rscpFindTag(info, "BAT_DCB_STATUS")[2],
                "temperatures": temperatures,
                "voltage": rscpFindTag(info, "BAT_DCB_VOLTAGE")[2],
                "voltageAvg30s": rscpFindTag(info, "BAT_DCB_VOLTAGE_AVG_30S")[2],
                "voltages": voltages,
                "warning": rscpFindTag(info, "BAT_DCB_WARNING")[2],
            }
            outObj["dcbs"][str(dcb)] = dcbobj
        return outObj

    def get_pvi_data(self, pviIndex=0, string=None, phase=None, keepAlive=False):
        """Polls the inverter data via rscp protocol locally

        Returns:
            Dictionary containing the pvi data structured as follows:
                {
                    'stringIndex': string index
                    'pviTracker': pvi Tracker
                    'acApparentPower': ac apparent power
                    'acCurrent': ac current
                    'acEnergyAll': ac energy all
                    'acPower': ac power
                    'acReactivePower': ac reactive power
                    'acVoltage': ac voltage
                    'dcCurrent': dc current
                    'dcPower': dc power
                    'dcVoltage': dc voltage
                    'deviceConnected': boolean if pvi is connected
                    'deviceInService': boolean if pvi is in service
                    'deviceWorking': boolean if pvi is working
                    'lastError': last error
                    'temperature': temperature
                }
        """

        req = self.sendRequest(
            (
                "PVI_REQ_DATA",
                "Container",
                [
                    ("PVI_INDEX", "Uint16", pviIndex),
                    ("PVI_REQ_AC_MAX_PHASE_COUNT", "None", None),
                    ("PVI_REQ_TEMPERATURE_COUNT", "None", None),
                    ("PVI_REQ_DC_MAX_STRING_COUNT", "None", None),
                    ("PVI_REQ_USED_STRING_COUNT", "None", None),
                    ("PVI_REQ_TYPE", "None", None),
                    ("PVI_REQ_SERIAL_NUMBER", "None", None),
                    ("PVI_REQ_VERSION", "None", None),
                    ("PVI_REQ_ON_GRID", "None", None),
                    ("PVI_REQ_STATE", "None", None),
                    ("PVI_REQ_LAST_ERROR", "None", None),
                    ("PVI_REQ_COS_PHI", "None", None),
                    ("PVI_REQ_VOLTAGE_MONITORING", "None", None),
                    ("PVI_REQ_POWER_MODE", "None", None),
                    ("PVI_REQ_SYSTEM_MODE", "None", None),
                    ("PVI_REQ_FREQUENCY_UNDER_OVER", "None", None),
                    ("PVI_REQ_MAX_TEMPERATURE", "None", None),
                    ("PVI_REQ_MIN_TEMPERATURE", "None", None),
                    ("PVI_REQ_AC_MAX_APPARENTPOWER", "None", None),
                    ("PVI_REQ_DEVICE_STATE", "None", None),
                ],
            ),
            keepAlive=True,
        )

        maxPhaseCount = int(rscpFindTag(req, "PVI_AC_MAX_PHASE_COUNT")[2])
        maxStringCount = int(rscpFindTag(req, "PVI_DC_MAX_STRING_COUNT")[2])
        usedStringCount = int(rscpFindTag(req, "PVI_USED_STRING_COUNT")[2])

        voltageMonitoring = rscpFindTag(req, "PVI_VOLTAGE_MONITORING")
        cosPhi = rscpFindTag(req, "PVI_COS_PHI")
        frequency = rscpFindTag(req, "PVI_FREQUENCY_UNDER_OVER")
        deviceState = rscpFindTag(req, "PVI_DEVICE_STATE")

        outObj = {
            "index": pviIndex,
            "type": rscpFindTag(req, "PVI_TYPE")[2],
            "serialNumber": rscpFindTag(req, "PVI_SERIAL_NUMBER")[2],
            "version": rscpFindTag(rscpFindTag(req, "PVI_VERSION"), "PVI_VERSION_MAIN")[
                2
            ],
            "onGrid": rscpFindTag(req, "PVI_ON_GRID")[2],
            "state": rscpFindTag(req, "PVI_STATE")[2],
            "lastError": rscpFindTag(req, "PVI_LAST_ERROR")[2],
            "cosPhi": {
                "active": rscpFindTag(cosPhi, "PVI_COS_PHI_IS_AKTIV")[2],
                "value": rscpFindTag(cosPhi, "PVI_COS_PHI_VALUE")[2],
                "excited": rscpFindTag(cosPhi, "PVI_COS_PHI_EXCITED")[2],
            },
            "voltageMonitoring": {
                "thresholdTop": rscpFindTag(
                    voltageMonitoring, "PVI_VOLTAGE_MONITORING_THRESHOLD_TOP"
                )[2],
                "thresholdBottom": rscpFindTag(
                    voltageMonitoring, "PVI_VOLTAGE_MONITORING_THRESHOLD_BOTTOM"
                )[2],
                "slopeUp": rscpFindTag(
                    voltageMonitoring, "PVI_VOLTAGE_MONITORING_SLOPE_UP"
                )[2],
                "slopeDown": rscpFindTag(
                    voltageMonitoring, "PVI_VOLTAGE_MONITORING_SLOPE_DOWN"
                )[2],
            },
            "powerMode": rscpFindTag(req, "PVI_POWER_MODE")[2],
            "systemMode": rscpFindTag(req, "PVI_SYSTEM_MODE")[2],
            "maxPhaseCount": maxPhaseCount,
            "maxStringCount": maxStringCount,
            "frequency": {
                "under": rscpFindTag(frequency, "PVI_FREQUENCY_UNDER")[2],
                "over": rscpFindTag(frequency, "PVI_FREQUENCY_OVER")[2],
            },
            "temperature": {
                "max": rscpFindTag(
                    rscpFindTag(req, "PVI_MAX_TEMPERATURE"), "PVI_VALUE"
                )[2],
                "min": rscpFindTag(
                    rscpFindTag(req, "PVI_MIN_TEMPERATURE"), "PVI_VALUE"
                )[2],
                "values": [],
            },
            "acMaxApparentPower": rscpFindTag(
                rscpFindTag(req, "PVI_AC_MAX_APPARENTPOWER"), "PVI_VALUE"
            )[2],
            "deviceState": {
                "connected": rscpFindTag(deviceState, "PVI_DEVICE_CONNECTED")[2],
                "working": rscpFindTag(deviceState, "PVI_DEVICE_WORKING")[2],
                "inService": rscpFindTag(deviceState, "PVI_DEVICE_IN_SERVICE")[2],
            },
            "phases": {},
            "strings": {},
        }

        temperatures = range(0, int(rscpFindTag(req, "PVI_TEMPERATURE_COUNT")[2]))
        for temperature in temperatures:
            req = self.sendRequest(
                (
                    "PVI_REQ_DATA",
                    "Container",
                    [
                        ("PVI_INDEX", "Uint16", pviIndex),
                        ("PVI_REQ_TEMPERATURE", "Uint16", temperature),
                    ],
                ),
                keepAlive=True,
            )
            outObj["temperature"]["values"].append(
                round(
                    rscpFindTag(rscpFindTag(req, "PVI_TEMPERATURE"), "PVI_VALUE")[2], 2
                )
            )

        if phase is None:
            phases = range(0, maxPhaseCount)
        elif isinstance(phase, list):
            phases = phase
        else:
            phases = [phase]

        for phase in phases:
            req = self.sendRequest(
                (
                    "PVI_REQ_DATA",
                    "Container",
                    [
                        ("PVI_INDEX", "Uint16", pviIndex),
                        ("PVI_REQ_AC_POWER", "Uint16", phase),
                        ("PVI_REQ_AC_VOLTAGE", "Uint16", phase),
                        ("PVI_REQ_AC_CURRENT", "Uint16", phase),
                        ("PVI_REQ_AC_APPARENTPOWER", "Uint16", phase),
                        ("PVI_REQ_AC_REACTIVEPOWER", "Uint16", phase),
                        ("PVI_REQ_AC_ENERGY_ALL", "Uint16", phase),
                        ("PVI_REQ_AC_ENERGY_GRID_CONSUMPTION", "Uint16", phase),
                    ],
                ),
                keepAlive=True,
            )
            phaseobj = {
                "power": round(
                    rscpFindTag(rscpFindTag(req, "PVI_AC_POWER"), "PVI_VALUE")[2], 2
                ),
                "voltage": round(
                    rscpFindTag(rscpFindTag(req, "PVI_AC_VOLTAGE"), "PVI_VALUE")[2], 2
                ),
                "current": round(
                    rscpFindTag(rscpFindTag(req, "PVI_AC_CURRENT"), "PVI_VALUE")[2], 2
                ),
                "apparentPower": round(
                    rscpFindTag(rscpFindTag(req, "PVI_AC_APPARENTPOWER"), "PVI_VALUE")[
                        2
                    ],
                    2,
                ),
                "reactivePower": round(
                    rscpFindTag(rscpFindTag(req, "PVI_AC_REACTIVEPOWER"), "PVI_VALUE")[
                        2
                    ],
                    2,
                ),
                "energyAll": round(
                    rscpFindTag(rscpFindTag(req, "PVI_AC_ENERGY_ALL"), "PVI_VALUE")[2],
                    2,
                ),
                "energyGridConsumption": round(
                    rscpFindTag(
                        rscpFindTag(req, "PVI_AC_ENERGY_GRID_CONSUMPTION"), "PVI_VALUE"
                    )[2],
                    2,
                ),
            }
            outObj["phases"][str(phase)] = phaseobj

        if string is None:
            strings = range(0, usedStringCount)
        elif isinstance(string, list):
            strings = string
        else:
            strings = [string]

        for string in strings:
            req = self.sendRequest(
                (
                    "PVI_REQ_DATA",
                    "Container",
                    [
                        ("PVI_INDEX", "Uint16", pviIndex),
                        ("PVI_REQ_DC_POWER", "Uint16", string),
                        ("PVI_REQ_DC_VOLTAGE", "Uint16", string),
                        ("PVI_REQ_DC_CURRENT", "Uint16", string),
                        ("PVI_REQ_DC_STRING_ENERGY_ALL", "Uint16", string),
                    ],
                ),
                keepAlive=keepAlive if string == strings else True,
            )
            stringobj = {
                "power": round(
                    rscpFindTag(rscpFindTag(req, "PVI_DC_POWER"), "PVI_VALUE")[2], 2
                ),
                "voltage": round(
                    rscpFindTag(rscpFindTag(req, "PVI_DC_VOLTAGE"), "PVI_VALUE")[2], 2
                ),
                "current": round(
                    rscpFindTag(rscpFindTag(req, "PVI_DC_CURRENT"), "PVI_VALUE")[2], 2
                ),
                "energyAll": round(
                    rscpFindTag(
                        rscpFindTag(req, "PVI_DC_STRING_ENERGY_ALL"), "PVI_VALUE"
                    )[2],
                    2,
                ),
            }
            outObj["strings"][str(string)] = stringobj

        return outObj

    def get_power_data(self, pmIndex=None, keepAlive=False):
        """Polls the power meter data via rscp protocol locally

        Returns:
            Dictionary containing the power data structured as follows:
                {
                    'maxPhasePower': max power of the device
                    'power': {
                        'L1': L1 power
                        'L2': L2 power
                        'L3': L3 power
                    }
                }
        """

        if pmIndex is None:
            pmIndex = self.pmIndex

        res = self.sendRequest(
            (
                "PM_REQ_DATA",
                "Container",
                [
                    ("PM_INDEX", "Uint16", pmIndex),
                    ("PM_REQ_POWER_L1", "None", None),
                    ("PM_REQ_POWER_L2", "None", None),
                    ("PM_REQ_POWER_L3", "None", None),
                    ("PM_REQ_VOLTAGE_L1", "None", None),
                    ("PM_REQ_VOLTAGE_L2", "None", None),
                    ("PM_REQ_VOLTAGE_L3", "None", None),
                    ("PM_REQ_ENERGY_L1", "None", None),
                    ("PM_REQ_ENERGY_L2", "None", None),
                    ("PM_REQ_ENERGY_L3", "None", None),
                    ("PM_REQ_MAX_PHASE_POWER", "None", None),
                    ("PM_REQ_ACTIVE_PHASES", "None", None),
                    ("PM_REQ_TYPE", "None", None),
                    ("PM_REQ_MODE", "None", None),
                ],
            ),
            keepAlive=keepAlive,
        )

        activePhasesChar = rscpFindTag(res, "PM_ACTIVE_PHASES")[2]
        activePhases = f"{activePhasesChar:03b}"

        outObj = {
            "power": {
                "L1": rscpFindTag(res, "PM_POWER_L1")[2],
                "L2": rscpFindTag(res, "PM_POWER_L2")[2],
                "L3": rscpFindTag(res, "PM_POWER_L3")[2],
            },
            "voltage": {
                "L1": rscpFindTag(res, "PM_VOLTAGE_L1")[2],
                "L2": rscpFindTag(res, "PM_VOLTAGE_L2")[2],
                "L3": rscpFindTag(res, "PM_VOLTAGE_L3")[2],
            },
            "energy": {
                "L1": rscpFindTag(res, "PM_ENERGY_L1")[2],
                "L2": rscpFindTag(res, "PM_ENERGY_L2")[2],
                "L3": rscpFindTag(res, "PM_ENERGY_L3")[2],
            },
            "maxPhasePower": rscpFindTag(res, "PM_MAX_PHASE_POWER")[2],
            "activePhases": activePhases,
            "type": rscpFindTag(res, "PM_TYPE")[2],
            "mode": rscpFindTag(res, "PM_MODE")[2],
        }
        return outObj

    def get_power_data_ext(self, pmIndexExt=None, keepAlive=False):
        """Polls the external power meter data via rscp protocol locally"""

        if pmIndexExt is None:
            pmIndexExt = self.pmIndexExt

        return get_power_data(pmIndexExt, keepAlive)

    def get_power_settings(self, keepAlive=False):
        """
        Get Power Settings
        Returns:
            Dictionary containing the condensed status information structured as follows:
           {
            'discharge_start_power': minimum power requested to enable discharge
            'maxChargePower': maximum charge power dependent on E3DC model
            'maxDischargePower': maximum discharge power dependent on E3DC model
            'powerSaveEnabled': status if power save is enabled
            'powerLimitsUsed': status if power limites are enabled
            'weatherForecastMode': Weather Forcast Mode
            'weatherRegulatedChargeEnabled': status if weather regulated charge is enabled
           }
        """

        res = self.sendRequest(
            ("EMS_REQ_GET_POWER_SETTINGS", "None", None), keepAlive=keepAlive
        )

        dischargeStartPower = rscpFindTag(res, "EMS_DISCHARGE_START_POWER")[2]
        maxChargePower = rscpFindTag(res, "EMS_MAX_CHARGE_POWER")[2]
        maxDischargePower = rscpFindTag(res, "EMS_MAX_DISCHARGE_POWER")[2]
        powerLimitsUsed = rscpFindTag(res, "EMS_POWER_LIMITS_USED")[2]
        powerSaveEnabled = rscpFindTag(res, "EMS_POWERSAVE_ENABLED")[2]
        weatherForecastMode = rscpFindTag(res, "EMS_WEATHER_FORECAST_MODE")[2]
        weatherRegulatedChargeEnabled = rscpFindTag(
            res, "EMS_WEATHER_REGULATED_CHARGE_ENABLED"
        )[2]

        outObj = {
            "dischargeStartPower": dischargeStartPower,
            "maxChargePower": maxChargePower,
            "maxDischargePower": maxDischargePower,
            "powerLimitsUsed": powerLimitsUsed,
            "powerSaveEnabled": powerSaveEnabled,
            "weatherForecastMode": weatherForecastMode,
            "weatherRegulatedChargeEnabled": weatherRegulatedChargeEnabled,
        }
        return outObj

    def set_power_limits(
        self,
        enable,
        max_charge=None,
        max_discharge=None,
        discharge_start=None,
        keepAlive=False,
    ):
        """
        Setting the SmartPower power limits
        Input:
            enable: True/False
            max_charge: maximum charge power
            max_discharge: maximum discharge power
            discharge_start: power where discharged is started
        Returns:
            0 if success
            -1 if error
            1 if one value is nonoptimal
        """

        if max_charge is None:
            max_charge = self.maxBatChargePower

        if max_discharge is None:
            max_discharge = self.maxBatDischargePower

        if discharge_start is None:
            discharge_start = self.startDischargeDefault

        if enable:
            res = self.sendRequest(
                (
                    "EMS_REQ_SET_POWER_SETTINGS",
                    "Container",
                    [
                        ("EMS_POWER_LIMITS_USED", "Bool", True),
                        ("EMS_MAX_DISCHARGE_POWER", "Uint32", max_discharge),
                        ("EMS_MAX_CHARGE_POWER", "Uint32", max_charge),
                        ("EMS_DISCHARGE_START_POWER", "Uint32", discharge_start),
                    ],
                ),
                keepAlive=keepAlive,
            )
        else:
            res = self.sendRequest(
                (
                    "EMS_REQ_SET_POWER_SETTINGS",
                    "Container",
                    [("EMS_POWER_LIMITS_USED", "Bool", False)],
                ),
                keepAlive=keepAlive,
            )

        # validate all return codes for each limit to be 0 for success, 1 for nonoptimal value and -1 for failure
        return_code = 0
        for result in res[2]:
            if result[2] == -1:
                return_code = -1
            elif result[2] == 1 and return_code == 0:
                return_code = 1

        return return_code

    def set_powersave(self, enable, keepAlive=False):
        """
        Setting the SmartPower power save
        Input:
            enable: True/False
        Returns:
            0 if success
            -1 if error
        """
        if enable:
            res = self.sendRequest(
                (
                    "EMS_REQ_SET_POWER_SETTINGS",
                    "Container",
                    [("EMS_POWERSAVE_ENABLED", "UChar8", 1)],
                ),
                keepAlive=keepAlive,
            )
        else:
            res = self.sendRequest(
                (
                    "EMS_REQ_SET_POWER_SETTINGS",
                    "Container",
                    [("EMS_POWERSAVE_ENABLED", "UChar8", 0)],
                ),
                keepAlive=keepAlive,
            )

        # validate return code for EMS_RES_POWERSAVE_ENABLED is 0
        if res[2][0][2] == 0:
            return 0
        else:
            return -1

    def set_weather_regulated_charge(self, enable, keepAlive=False):
        """
        Setting the SmartCharge weather regulated charge
        Input:
            enable: True/False
        Returns:
            0 if success
            -1 if error
        """
        if enable:
            res = self.sendRequest(
                (
                    "EMS_REQ_SET_POWER_SETTINGS",
                    "Container",
                    [("EMS_WEATHER_REGULATED_CHARGE_ENABLED", "UChar8", 1)],
                ),
                keepAlive=keepAlive,
            )
        else:
            res = self.sendRequest(
                (
                    "EMS_REQ_SET_POWER_SETTINGS",
                    "Container",
                    [("EMS_WEATHER_REGULATED_CHARGE_ENABLED", "UChar8", 0)],
                ),
                keepAlive=keepAlive,
            )

        # validate return code for EMS_RES_WEATHER_REGULATED_CHARGE_ENABLED is 0
        if res[2][0][2] == 0:
            return 0
        else:
            return -1
