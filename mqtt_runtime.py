"""MQTT startup that tolerates a broker starting after its clients."""


def start_mqtt(client, broker, port):
    client.reconnect_delay_set(min_delay=1, max_delay=30)
    client.connect_async(broker, port)
    client.loop_start()


def run_mqtt(client, broker, port):
    client.reconnect_delay_set(min_delay=1, max_delay=30)
    client.connect_async(broker, port)
    try:
        client.loop_forever(retry_first_connection=True)
    except KeyboardInterrupt:
        pass
    finally:
        client.disconnect()
