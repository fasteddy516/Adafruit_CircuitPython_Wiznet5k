# SPDX-FileCopyrightText: 2009 Jordan Terrell (blog.jordanterrell.com)
# SPDX-FileCopyrightText: 2020 Brent Rubell for Adafruit Industries
# SPDX-FileCopyrightText: 2021 Patrick Van Oosterwijck @ Silicognition LLC
# SPDX-FileCopyrightText: 2022 Martin Stephens
#
# SPDX-License-Identifier: MIT

"""
`adafruit_wiznet5k_dhcp`
================================================================================

Pure-Python implementation of Jordan Terrell's DHCP library v0.3

* Author(s): Jordan Terrell, Brent Rubell, Martin Stephens

"""
from __future__ import annotations

try:
    from typing import TYPE_CHECKING, Optional, Union, Tuple, Sequence

    if TYPE_CHECKING:
        from adafruit_wiznet5k.adafruit_wiznet5k import WIZNET5K
except ImportError:
    pass

import gc
import time
from random import randint
from micropython import const

# DHCP State Machine
STATE_INIT = const(0x01)
STATE_SELECTING = const(0x02)
STATE_REQUESTING = const(0x03)
STATE_BOUND = const(0x04)
STATE_RENEWING = const(0x05)
STATE_REBINDING = const(0x06)

# DHCP Message Types
DHCP_DISCOVER = const(1)
DHCP_OFFER = const(2)
DHCP_REQUEST = const(3)
DHCP_DECLINE = const(4)
DHCP_ACK = const(5)
DHCP_NAK = const(6)
DHCP_RELEASE = const(7)
DHCP_INFORM = const(8)

# DHCP Message OP Codes
DHCP_BOOT_REQUEST = const(0x01)
DHCP_BOOT_REPLY = const(0x02)

DHCP_HTYPE10MB = const(0x01)
DHCP_HTYPE100MB = const(0x02)

DHCP_HLENETHERNET = const(0x06)
DHCP_HOPS = const(0x00)

MAGIC_COOKIE = b"c\x82Sc"  # Four bytes 99.130.83.99
MAX_DHCP_OPT = const(0x10)

# Default DHCP Server port
DHCP_SERVER_PORT = const(67)
# DHCP Lease Time, in seconds
DEFAULT_LEASE_TIME = const(900)
BROADCAST_SERVER_ADDR = b"\xff\xff\xff\xff"  # (255.255.255.255)
UNASSIGNED_IP_ADDR = b"\x00\x00\x00\x00"  # (0.0.0.0)

# DHCP Response Options
MSG_TYPE = 53
SUBNET_MASK = 1
ROUTERS_ON_SUBNET = 3
DNS_SERVERS = 6
DHCP_SERVER_ID = 54
T1_VAL = 58
T2_VAL = 59
LEASE_TIME = 51
OPT_END = 255

# Packet buffer
BUFF_LENGTH = 318
_BUFF = bytearray(BUFF_LENGTH)


def _debugging_message(message: Union[Exception, str], debugging: bool) -> None:
    """Helper function to print debugging messages."""
    if debugging:
        print(message)


class DHCP:
    """Wiznet5k DHCP Client.

    Implements a DHCP client using a finite state machine (FSM). This allows the DHCP client
    to run in a non-blocking mode suitable for CircuitPython.

    The DHCP client obtains a lease and maintains it. The process of obtaining the initial
    lease is run in a blocking mode, as several messages must be exchanged with the DHCP
    server. Once the lease has been allocated, lease maintenance can be performed in
    non-blocking mode as nothing needs to be done until it is time to reallocate the
    lease. Renewing or rebinding is a simpler process which may be repeated periodically
    until successful. If the lease expires, the client attempts to obtain a new lease in
    blocking mode when the maintenance routine is run.

    In most circumstances, call `DHCP.request_lease` to obtain a lease, then periodically call
    `DHCP.maintain_lease` in non-blocking mode so that the FSM can check whether the lease
    needs to be renewed, and can then renew it.

    Since DHCP uses UDP, messages may be lost. The DHCP protocol uses exponential backoff
    for retrying. Retries occur after 4, 8, and 16 seconds (the final retry is followed by
    a wait of 32 seconds) so it will take about a minute to decide that no DHCP server
    is available.

    The DHCP client cannot check whether the allocated IP address is already in use because
    the ARP protocol is not available. Therefore, it is possible that the IP address
    allocated by the server has been manually assigned to another device. In most cases,
    the DHCP server will make this check before allocating an address, but some do not.

    The DHCPRELEASE message is not implemented. The DHCP protocol does not require it and
    DHCP servers can handle disappearing clients and clients that ask for 'replacement'
    IP addresses.
    """

    # pylint: disable=too-many-arguments, too-many-instance-attributes, invalid-name
    def __init__(
        self,
        eth: WIZNET5K,
        mac_address: Sequence[Union[int, bytes]],
        hostname: Optional[str] = None,
        response_timeout: float = 30.0,
        debug: bool = False,
    ) -> None:
        """
        :param adafruit_wiznet5k.WIZNET5K eth: Wiznet 5k object
        :param Sequence[Union[int, bytes]] mac_address: Hardware MAC address.
        :param Optional[str] hostname: The desired hostname, with optional {} to fill
            in the MAC address, defaults to None.
        :param float response_timeout: DHCP Response timeout in seconds, defaults to 30.
        :param bool debug: Enable debugging output.
        """
        self._debug = debug
        _debugging_message("Initialising DHCP instance.", self._debug)
        self._response_timeout = response_timeout

        # Prevent buffer overrun in send_dhcp_message()
        if len(mac_address) != 6:
            raise ValueError("The MAC address must be 6 bytes.")
        self._mac_address = mac_address

        # Set socket interface
        self._eth = eth
        self._wiz_sock = None

        # DHCP state machine
        self._dhcp_state = STATE_INIT
        self._transaction_id = randint(1, 0x7FFFFFFF)
        self._start_time = 0.0
        self._blocking = False
        self._renew = False

        # DHCP binding configuration
        self.dhcp_server_ip = BROADCAST_SERVER_ADDR
        self.local_ip = UNASSIGNED_IP_ADDR
        self.gateway_ip = UNASSIGNED_IP_ADDR
        self.subnet_mask = UNASSIGNED_IP_ADDR
        self.dns_server_ip = UNASSIGNED_IP_ADDR

        # Lease configuration
        self._lease_time = 0
        self._t1 = 0
        self._t2 = 0

        # Host name
        mac_string = "".join("{:02X}".format(o) for o in mac_address)
        self._hostname = bytes(
            (hostname or "WIZnet{}").split(".")[0].format(mac_string)[:42], "utf-8"
        )

    def request_dhcp_lease(self) -> bool:
        """Request to renew or acquire a DHCP lease."""
        _debugging_message("Requesting DHCP lease.", self._debug)
        self._dhcp_state_machine(blocking=True)
        return self._dhcp_state == STATE_BOUND

    def maintain_dhcp_lease(self, blocking: bool = False) -> None:
        """Maintain DHCP lease"""
        _debugging_message(
            "Maintaining lease with blocking = {}".format(blocking), self._debug
        )
        self._dhcp_state_machine(blocking=blocking)

    def _dsm_reset(self) -> None:
        """Close the socket and set attributes to default values used by the
        state machine INIT state."""
        _debugging_message("Resetting DHCP state machine.", self._debug)
        self._socket_release()
        self._dhcp_connection_setup()
        self.dhcp_server_ip = BROADCAST_SERVER_ADDR
        self._eth.ifconfig = (
            UNASSIGNED_IP_ADDR,
            UNASSIGNED_IP_ADDR,
            UNASSIGNED_IP_ADDR,
            UNASSIGNED_IP_ADDR,
        )
        self.gateway_ip = UNASSIGNED_IP_ADDR
        self.local_ip = UNASSIGNED_IP_ADDR
        self.subnet_mask = UNASSIGNED_IP_ADDR
        self.dns_server_ip = UNASSIGNED_IP_ADDR
        self._renew = False
        self._increment_transaction_id()
        self._start_time = time.monotonic()

    def _socket_release(self) -> None:
        """Close the socket if it exists."""
        _debugging_message("Releasing socket.", self._debug)
        if self._wiz_sock:
            self._eth.socket_close(self._wiz_sock)
            self._wiz_sock = None

    def _dhcp_connection_setup(self, timeout: float = 5.0) -> None:
        """Initialise a UDP socket.

        Attempt to initialise a UDP socket. If the finite state machine (FSM) is in
        blocking mode, repeat failed attempts until a socket is initialised or
        the operation times out, then raise an exception. If the FSM is in non-blocking
        mode, ignore the error and return.

        :param int timeout: Time to keep retrying if the FSM is in blocking mode.
            Defaults to 5.

        :raises TimeoutError: If the FSM is in blocking mode and a socket cannot be
            initialised.
        """
        stop_time = time.monotonic() + timeout
        _debugging_message("Creating new socket instance for DHCP.", self._debug)
        while self._wiz_sock is None and time.monotonic() < stop_time:
            self._wiz_sock = self._eth.get_socket()
            if self._wiz_sock == 0xFF:
                self._wiz_sock = None
        while time.monotonic() < stop_time:
            self._eth.write_snmr(self._wiz_sock, 0x02)  # Set UDP connection
            self._eth.write_sock_port(self._wiz_sock, 68)  # Set DHCP client port.
            self._eth.write_sncr(self._wiz_sock, 0x01)  # Open the socket.
            while (
                self._eth.read_sncr(self._wiz_sock) != 0
            ):  # Wait for command to complete.
                time.sleep(0.001)
            if self._eth.read_snsr(self._wiz_sock) == bytes([0x22]):
                self._eth.write_sndport(2, DHCP_SERVER_PORT)
                return
        self._wiz_sock = None
        raise RuntimeError("Unable to initialize UDP socket.")

    def _increment_transaction_id(self) -> None:
        """Increment the transaction ID and roll over from 0x7fffffff to 0."""
        _debugging_message("Incrementing transaction ID", self._debug)
        self._transaction_id = (self._transaction_id + 1) & 0x7FFFFFFF

    def _next_retry_time(self, *, attempt: int, interval: int = 4) -> float:
        """Calculate a retry stop time.

        The interval is calculated as an exponential fallback with a random variation to
        prevent DHCP packet collisions. This timeout is intended to be compared with
        time.monotonic().

        :param int attempt: The current attempt, used as the exponent for calculating the
            timeout.
        :param int interval: The base retry interval in seconds. Defaults to 4 as per the
            DHCP standard for Ethernet connections. Minimum value 2, defaults to 4.

        :returns float: The timeout in time.monotonic() seconds.

        :raises ValueError: If the interval is not > 1 second as this could return a zero or
            negative delay.
        """
        _debugging_message(
            "Calculating next retry time and incrementing retries.", self._debug
        )
        if interval <= 1:
            raise ValueError("Retry interval must be > 1 second.")
        delay = 2**attempt * interval + randint(-1, 1) + time.monotonic()
        return delay

    def _receive_dhcp_response(self, timeout: float) -> int:
        """
        Receive data from the socket in response to a DHCP query.

        Reads data from the buffer until a viable minimum packet size has been
        received or the operation times out. If a viable packet is received, it is
        stored in the global buffer and the number of bytes received is returned.
        If the packet is too short, it is discarded and zero is returned. The
        maximum packet size is limited by the size of the global buffer.

        :returns int: The number of bytes stored in the global buffer.
        """
        _debugging_message("Receiving a DHCP response.", self._debug)
        # DHCP returns the query plus additional data. The query length is 236 bytes.
        minimum_packet_length = 236
        buffer = bytearray(b"")
        bytes_read = 0
        while bytes_read <= minimum_packet_length and time.monotonic() < timeout:
            buffer.extend(
                self._eth.read_udp(self._wiz_sock, BUFF_LENGTH - bytes_read)[1]
            )
            bytes_read = len(buffer)
            if bytes_read == BUFF_LENGTH:
                break
        _debugging_message("Received {} bytes".format(bytes_read), self._debug)
        if bytes_read < minimum_packet_length:
            bytes_read = 0
        else:
            _BUFF[:bytes_read] = buffer
            _BUFF[bytes_read:] = bytearray(BUFF_LENGTH - bytes_read)
        del buffer
        gc.collect()
        return bytes_read

    def _process_messaging_states(self, *, message_type: int):
        """
        Process a message while the FSM is in SELECTING or REQUESTING state.

        Check the message and update the FSM state if it is a valid type.

        :param int message_type: The type of message received from the DHCP server.

        :returns bool: True if the message was valid for the current state.
        """
        if self._dhcp_state == STATE_SELECTING and message_type == DHCP_OFFER:
            _debugging_message("FSM state is SELECTING with valid OFFER.", self._debug)
            self._dhcp_state = STATE_REQUESTING
        elif self._dhcp_state == STATE_REQUESTING:
            _debugging_message("FSM state is REQUESTING.", self._debug)
            if message_type == DHCP_NAK:
                _debugging_message(
                    "Message is NAK, setting FSM state to INIT.", self._debug
                )
                self._dhcp_state = STATE_INIT
            elif message_type == DHCP_ACK:
                _debugging_message(
                    "Message is ACK, setting FSM state to BOUND.", self._debug
                )
                if self._lease_time == 0:
                    self._lease_time = DEFAULT_LEASE_TIME
                self._t1 = self._start_time + self._lease_time // 2
                self._t2 = self._start_time + self._lease_time - self._lease_time // 8
                self._lease_time += self._start_time
                self._increment_transaction_id()
                self._renew = False
                self._dhcp_state = STATE_BOUND

    def _handle_dhcp_message(self) -> int:
        """Send, receive and process DHCP message. Update the finite state machine (FSM).

        Send a message and wait for a response from the DHCP server, resending on an
        exponential fallback schedule matching the DHCP standard if no response is received.
        Only called when the FSM is in SELECTING or REQUESTING states.

        :returns int: The DHCP message type, or 0 if no message received in non-blocking
            or renewing states.

        :raises ValueError: If the function is not called from SELECTING or BLOCKING FSM
            states.
        :raises TimeoutError: If the FSM is in blocking mode and no valid response has
            been received before the timeout expires.
        """
        _debugging_message("Processing SELECTING or REQUESTING state.", self._debug)
        if self._dhcp_state == STATE_SELECTING:
            msg_type_out = DHCP_DISCOVER
        elif self._dhcp_state == STATE_REQUESTING:
            msg_type_out = DHCP_REQUEST
        else:
            raise ValueError(
                "FSM can only send messages while in SELECTING or REQUESTING states."
            )
        for attempt in range(4):  # Initial attempt plus 3 retries.
            message_length = self._generate_dhcp_message(message_type=msg_type_out)
            self._eth.write_sndipr(self._wiz_sock, self.dhcp_server_ip)
            self._eth.socket_write(self._wiz_sock, _BUFF[:message_length])
            next_resend = self._next_retry_time(attempt=attempt)
            while time.monotonic() < next_resend:
                if self._receive_dhcp_response(next_resend):
                    try:
                        msg_type_in = self._parse_dhcp_response()
                        _debugging_message(
                            "Received message type {}".format(msg_type_in), self._debug
                        )
                        return msg_type_in
                    except ValueError as error:
                        _debugging_message(error, self._debug)
                if not self._blocking or self._renew:
                    _debugging_message(
                        "No message, nonblocking or renewing, exiting loop.",
                        self._debug,
                    )
                    return 0  # Did not receive a response in a single attempt.
        raise TimeoutError(
            "No response from DHCP server after {} retries.".format(attempt)
        )

    def _dhcp_state_machine(self, *, blocking: bool = False) -> None:
        """
        A finite state machine to allow the DHCP lease to be managed without blocking
        the main program. The initial lease...
        """
        _debugging_message(
            "DHCP FSM called with blocking={}".format(blocking), self._debug
        )
        _debugging_message(
            "FSM initial state is {}".format(self._dhcp_state), self._debug
        )
        self._blocking = blocking
        while True:
            if self._dhcp_state == STATE_BOUND:
                now = time.monotonic()
                if now < self._t1:
                    _debugging_message(
                        "No timers have expired. Exiting FSM.", self._debug
                    )
                    self._socket_release()
                    return
                if now > self._lease_time:
                    _debugging_message(
                        "Lease has expired, switching state to INIT.", self._debug
                    )
                    self._blocking = True
                    self._dhcp_state = STATE_INIT
                elif now > self._t2:
                    _debugging_message(
                        "T2 has expired, switching state to REBINDING.", self._debug
                    )
                    self._dhcp_state = STATE_REBINDING
                else:
                    _debugging_message(
                        "T1 has expired, switching state to RENEWING.", self._debug
                    )
                    self._dhcp_state = STATE_RENEWING

            if self._dhcp_state == STATE_RENEWING:
                self._renew = True
                self._dhcp_connection_setup()
                self._start_time = time.monotonic()
                self._dhcp_state = STATE_REQUESTING

            if self._dhcp_state == STATE_REBINDING:
                self._renew = True
                self.dhcp_server_ip = BROADCAST_SERVER_ADDR
                self._dhcp_connection_setup()
                self._start_time = time.monotonic()
                self._dhcp_state = STATE_REQUESTING

            if self._dhcp_state == STATE_INIT:
                self._dsm_reset()
                self._dhcp_state = STATE_SELECTING

            if self._dhcp_state == STATE_SELECTING:
                self._process_messaging_states(message_type=self._handle_dhcp_message())

            if self._dhcp_state == STATE_REQUESTING:
                self._process_messaging_states(message_type=self._handle_dhcp_message())

            if self._renew:
                _debugging_message(
                    "Lease has not expired, resetting state to BOUND and exiting FSM.",
                    self._debug,
                )
                self._dhcp_state = STATE_BOUND

    def _generate_dhcp_message(
        self,
        *,
        message_type: int,
        broadcast: bool = False,
        renew: bool = False,
    ) -> int:
        """
        Assemble a DHCP message. The content will vary depending on which type of
            message is being sent and whether the lease is new or being renewed.

        :param int message_type: Type of message to generate.
        :param bool broadcast: Used to set the flag requiring a broadcast reply from the
            DHCP server. Defaults to False which matches the DHCP standard.
        :param bool renew: Set True for renewing and rebinding operations, defaults to False.

        :returns int: The length of the DHCP message.
        """

        def option_writer(
            offset: int, option_code: int, option_data: Union[Tuple[int, ...], bytes]
        ) -> int:
            """Helper function to set DHCP option data for a DHCP
            message.

            :param int offset: Pointer to start of a DHCP option.
            :param int option_code: Type of option to add.
            :param Tuple[int] option_data: The data for the option.

            :returns int: Pointer to start of next option.
            """
            _BUFF[offset] = option_code
            data_length = len(option_data)
            offset += 1
            _BUFF[offset] = data_length
            offset += 1
            data_end = offset + data_length
            _BUFF[offset:data_end] = bytes(option_data)
            return data_end

        # global _BUFF  # pylint: disable=global-variable-not-assigned
        _BUFF[:] = bytearray(BUFF_LENGTH)
        # OP.HTYPE.HLEN.HOPS
        _BUFF[0:4] = bytes(
            [DHCP_BOOT_REQUEST, DHCP_HTYPE10MB, DHCP_HLENETHERNET, DHCP_HOPS]
        )
        # Transaction ID (xid)
        _BUFF[4:8] = self._transaction_id.to_bytes(4, "big")
        # Seconds elapsed
        _BUFF[8:10] = int(time.monotonic() - self._start_time).to_bytes(2, "big")
        # Flags (only bit 0 is used, all other bits must be 0)
        if broadcast:
            _BUFF[10] = 0b10000000
        if renew:
            _BUFF[12:16] = bytes(self.local_ip)
        # chaddr
        _BUFF[28:34] = self._mac_address
        # Magic Cookie
        _BUFF[236:240] = MAGIC_COOKIE

        # Set DHCP options.
        pointer = 240

        # Option - DHCP Message Type
        pointer = option_writer(
            offset=pointer, option_code=53, option_data=(message_type,)
        )
        # Option - Host Name
        pointer = option_writer(
            offset=pointer, option_code=12, option_data=self._hostname
        )
        if message_type == DHCP_REQUEST:
            # Request subnet mask, router and DNS server.
            pointer = option_writer(
                offset=pointer, option_code=55, option_data=(1, 3, 6)
            )
            # Set Requested IP Address to offered IP address.
            pointer = option_writer(
                offset=pointer, option_code=50, option_data=self.local_ip
            )
            # Set Server ID to chosen DHCP server IP address.
            pointer = option_writer(
                offset=pointer, option_code=54, option_data=self.dhcp_server_ip
            )
        _BUFF[pointer] = 0xFF
        return pointer + 1

    def _parse_dhcp_response(
        self,
    ) -> int:
        """Parse DHCP response from DHCP server.

        Check that the message is for this client. Extract data from the fixed positions
         in the first 236 bytes of the message, then cycle through the options for
         additional data.

        :returns Tuple[int, bytearray]: DHCP packet type and ID.

        :raises ValueError: Checks that the message is a reply, the transaction ID
        matches, a client ID exists and the 'magic cookie' is set. If any of these tests
        fail or no message type is found in the options, raises a ValueError.
        """
        # pylint: disable=too-many-branches
        def option_reader(pointer: int) -> Tuple[int, int, bytes]:
            """Helper function to extract DHCP option data from a
            response.

            :param int pointer: Pointer to start of a DHCP option.

            :returns Tuple[int, int, bytes]: Pointer to next option,
                option type, and option data.
            """
            option_type = _BUFF[pointer]
            pointer += 1
            data_length = _BUFF[pointer]
            pointer += 1
            data_end = pointer + data_length
            option_data = _BUFF[pointer:data_end]
            return data_end, option_type, option_data

        # Validate OP
        if _BUFF[0] != DHCP_BOOT_REPLY:
            raise ValueError("DHCP message is not the expected DHCP Reply.")
        # Confirm transaction IDs match.
        xid = _BUFF[4:8]
        if xid != self._transaction_id.to_bytes(4, "big"):
            raise ValueError("DHCP response ID mismatch.")
        # Set the IP address to Claddr
        self.local_ip = tuple(_BUFF[16:20])
        # Check that there is a client ID.
        if _BUFF[28:34] == b"\x00\x00\x00\x00\x00\x00":
            raise ValueError("No client hardware MAC address in the response.")
        # Check for the magic cookie.
        if _BUFF[236:240] != MAGIC_COOKIE:
            raise ValueError("No DHCP Magic Cookie in the response.")

        # Parse options
        msg_type = None
        ptr = 240
        while _BUFF[ptr] != OPT_END:
            ptr, data_type, data = option_reader(ptr)
            if data_type == MSG_TYPE:
                msg_type = data[0]
            elif data_type == SUBNET_MASK:
                self.subnet_mask = tuple(data)
            elif data_type == DHCP_SERVER_ID:
                self.dhcp_server_ip = tuple(data)
            elif data_type == LEASE_TIME:
                self._lease_time = int.from_bytes(data, "big")
            elif data_type == ROUTERS_ON_SUBNET:
                self.gateway_ip = tuple(data[:4])
            elif data_type == DNS_SERVERS:
                self.dns_server_ip = tuple(data[:4])
            elif data_type == T1_VAL:
                self._t1 = int.from_bytes(data, "big")
            elif data_type == T2_VAL:
                self._t2 = int.from_bytes(data, "big")
            elif data_type == 0:
                break

        _debugging_message(
            "Msg Type: {}\nSubnet Mask: {}\nDHCP Server IP: {}\nDNS Server IP: {}\
                  \nGateway IP: {}\nLocal IP: {}\nT1: {}\nT2: {}\nLease Time: {}".format(
                msg_type,
                self.subnet_mask,
                self.dhcp_server_ip,
                self.dns_server_ip,
                self.gateway_ip,
                self.local_ip,
                self._t1,
                self._t2,
                self._lease_time,
            ),
            self._debug,
        )
        gc.collect()
        if msg_type is None:
            raise ValueError("No valid message type in response.")
        return msg_type
