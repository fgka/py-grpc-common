# vim: ai:sw=4:ts=4:sta:et:fo=croql
"""
Source: https://github.com/grpc/grpc/blob/master/examples/python/helloworld/greeter_client.py
"""
import logging

import grpc_common

import test_service_pb2
import test_service_pb2_grpc


def run():
    channel = grpc_common.create_grpc_channel(
        service_url='http://localhost:50051', pb2_module=test_service_pb2
    )
    stub = test_service_pb2_grpc.GreeterStub(channel)
    response = stub.SayHello(test_service_pb2.HelloRequest(name='you'))
    print(f'Greeter client received: {response.message}')


if __name__ == '__main__':
    logging.basicConfig()
    logging.getLogger().setLevel(logging.DEBUG)
    run()
