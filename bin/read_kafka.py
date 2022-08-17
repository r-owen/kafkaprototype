#!/usr/bin/env python

from __future__ import annotations

import argparse
import asyncio
import base64
import concurrent.futures

# import collections
import enum
import os
import time
import types
import typing

import aiohttp

from confluent_kafka import Consumer
from confluent_kafka.admin import AdminClient, ConfigResource, NewTopic
from confluent_kafka.error import KafkaError
from kafkit.registry.aiohttp import RegistryApi
from kafkit.registry import Deserializer
import numpy as np

import kafkaprototype


class PostProcess(enum.Enum):
    NONE = enum.auto()
    DATACLASS = enum.auto()
    PYDANTIC = enum.auto()
    SIMPLE_NAMESPACE = enum.auto()


POST_PROCESS_DICT = {item.name.lower(): item for item in PostProcess}


async def main() -> None:
    parser = argparse.ArgumentParser(
        "Read and print messages for one topic of one SAL component to Kafka."
    )
    parser.add_argument("component", help="SAL component name")
    parser.add_argument(
        "topic",
        nargs="+",
        help="Topic attribute names, e.g. evt_summaryState cmd_start",
    )
    parser.add_argument(
        "-n",
        "--number",
        type=int,
        default=10,
        help="Number of messages to read; 0 for no limit.",
    )
    parser.add_argument(
        "-t",
        "--time",
        action="store_true",
        help="Measure the elapsed time. This requires number > 1.",
    )
    parser.add_argument(
        "--max_history_read",
        type=int,
        default=1000,
        help="The max number of historical samples to read for indexed SAL components.",
    )
    parser.add_argument(
        "--partitions",
        type=int,
        default=1,
        help="The number of partitions per topic.",
    )
    parser.add_argument(
        "--postprocess",
        choices=POST_PROCESS_DICT.keys(),
        default="dataclass",
        help="How to handle the received data",
    )
    args = parser.parse_args()
    if args.time and args.number == 1:
        raise ValueError("You must specify --number > 1 with --time")
    print(f"Parsing info for component {args.component}")
    component_info = kafkaprototype.ComponentInfo(args.component)
    print(f"Obtaining info for topic {args.topic}")
    topic_infos = [component_info.topics[sal_name] for sal_name in args.topic]
    models = {
        topic_info.attr_name: topic_info.make_pydantic_model()
        for topic_info in topic_infos
    }
    data_classes = {
        topic_info.attr_name: topic_info.make_dataclass() for topic_info in topic_infos
    }
    post_process = POST_PROCESS_DICT[args.postprocess]
    delays = []
    with aiohttp.TCPConnector(limit_per_host=20) as connector:
        http_session = aiohttp.ClientSession(connector=connector)
        print("Create RegistryApi")
        registry = RegistryApi(url="http://schema-registry:8081", session=http_session)
        print("Create a deserializer")
        deserializer = Deserializer(registry=registry)
        print("Create a consumer")

        # Dict of kafka name: TopicInfo
        kafka_name_topic_info: typing.Dict[str, kafkaprototype.TopicInfo] = dict()
        for topic_info in topic_infos:
            avro_schema = topic_info.make_avro_schema()
            schema_id = await registry.register_schema(
                schema=avro_schema, subject=topic_info.avro_subject
            )
            kafka_name_topic_info[topic_info.kafka_name] = topic_info
            print(
                f"Registered schema with subject={topic_info.avro_subject} with ID {schema_id}"
            )

        all_topics = [topic_info.kafka_name for topic_info in topic_infos]

        # Create missing topics
        print("Create a broker client")
        broker_client = AdminClient({"bootstrap.servers": "broker:29092"})
        extra_topics = all_topics[:] + ["not_a_topic_name"]
        resource_list = [
            ConfigResource(restype=ConfigResource.Type.TOPIC, name=name)
            for name in extra_topics
        ]
        config_future_dict = broker_client.describe_configs(resource_list)
        add_topics = []
        for config, future in config_future_dict.items():
            topic_name = config.name
            exception = future.exception()
            if exception is not None:
                if exception.args[0].code() == KafkaError.UNKNOWN_TOPIC_OR_PART:
                    add_topics.append(
                        NewTopic(
                            topic_name,
                            num_partitions=args.partitions,
                            replication_factor=1,
                        )
                    )
                else:
                    print(f"Unknown issue with topic {topic_name}: {exception!r}")

        metadata = broker_client.list_topics(timeout=10)
        existing_topic_names = set(metadata.topics.keys())
        new_topic_names = sorted(set(all_topics) - existing_topic_names)
        if new_topic_names:
            print(f"Create topics: {new_topic_names}")
            new_topic_metadata = [
                NewTopic(
                    topic_name,
                    num_partitions=args.partitions,
                    replication_factor=1,
                )
                for topic_name in new_topic_names
            ]
            fs = broker_client.create_topics(new_topic_metadata)
            for topic_name, future in fs.items():
                try:
                    future.result()  # The result itself is None
                except Exception as e:
                    print(f"Failed to create topic {topic_name}: {e!r}")
                    raise

        random_str = base64.urlsafe_b64encode(os.urandom(12)).decode().replace("=", "_")
        consumer = Consumer(
            {"group.id": random_str, "bootstrap.servers": "broker:29092"}
        )
        consumer.subscribe(all_topics)

        def blocking_read():
            while True:
                message = consumer.poll(timeout=0.1)
                if message is not None:
                    error = message.error()
                    if error is not None:
                        raise error
                    return message

        loop = asyncio.get_running_loop()
        with concurrent.futures.ThreadPoolExecutor() as pool:
            i = 0
            while True:
                message = await loop.run_in_executor(pool, blocking_read)
                name = message.topic()
                raw_data = message.value()
                i += 1
                full_data = await deserializer.deserialize(raw_data)
                topic_info = kafka_name_topic_info[name]
                data_dict = full_data["message"]
                current_tai = time.time()
                data_dict["private_rcvStamp"] = current_tai
                delays.append(current_tai - data_dict["private_sndStamp"])
                if post_process == PostProcess.NONE:
                    pass
                elif post_process == PostProcess.DATACLASS:
                    DataClass = data_classes[topic_info.attr_name]
                    processed_data = DataClass(**data_dict)
                elif post_process == PostProcess.PYDANTIC:
                    Model = models[topic_info.attr_name]
                    processed_data = Model(**data_dict)
                elif post_process == PostProcess.SIMPLE_NAMESPACE:
                    processed_data = types.SimpleNamespace(**data_dict)
                else:
                    raise RuntimeError("Unsupported value of post_process")
                if not args.time:
                    print(f"read [{i}]: {processed_data!r}")
                if args.number > 0 and i >= args.number:
                    break
                # Don't start timing until the first message is processed,
                # to avoid delays in starting the producer
                # (and then to measure full read and process cycles).
                if i == 1:
                    t0 = time.time()

            dt = time.time() - t0
            if args.time:
                delays = np.array(delays)
                print(f"Read {(i-1)/dt:0.1f} messages/second: {args}")
                print(
                    f"Delay mean = {delays.mean():0.3f}, stdev = {delays.std():0.3f}, "
                    f"min = {delays.min():0.3f}, max = {delays.max():0.3f} seconds"
                )


asyncio.run(main())
