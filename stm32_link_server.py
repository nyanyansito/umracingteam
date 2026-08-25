import socket
import time

from stm32_simulator import generate_telemetry
from telemetry_packet import encode_fast_telemetry


HOST = "localhost"
PORT = 9000

UPDATE_RATE_HZ = 10
UPDATE_INTERVAL_SECONDS = 1 / UPDATE_RATE_HZ


def serve_connection(connection):
    """Stream telemetry packets to one connected ESP32 client.

    This stands in for the physical STM32 UART TX line: raw bytes go
    out continuously with no message boundaries, exactly as a real
    UART peripheral would send them. It is the receiving side's job
    (the ESP32) to find packet boundaries in the stream.
    """
    start_time = time.monotonic()
    sequence = 0

    try:
        while True:
            elapsed = time.monotonic() - start_time
            telemetry = generate_telemetry(sequence, elapsed)
            packet = encode_fast_telemetry(telemetry)

            connection.sendall(packet)

            sequence = (sequence + 1) % 65536
            time.sleep(UPDATE_INTERVAL_SECONDS)

    except (BrokenPipeError, ConnectionResetError, OSError):
        # The ESP32 side disconnected (or its socket errored out).
        # This is expected during reconnection testing, not a crash.
        return


def run_server():
    server_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server_socket.bind((HOST, PORT))
    server_socket.listen(1)

    print(f"Virtual STM32 UART link listening on {HOST}:{PORT}")
    print("Waiting for the virtual ESP32 to connect...")
    print("Press Control + C to stop")

    try:
        while True:
            connection, address = server_socket.accept()
            print(f"Virtual ESP32 connected from {address}")

            serve_connection(connection)
            connection.close()

            print("Virtual ESP32 disconnected. Waiting for reconnect...")

    finally:
        server_socket.close()


if __name__ == "__main__":
    try:
        run_server()
    except KeyboardInterrupt:
        print()
        print("Virtual STM32 UART link stopped")