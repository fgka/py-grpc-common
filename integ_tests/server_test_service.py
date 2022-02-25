# vim: ai:sw=4:ts=4:sta:et:fo=croql
"""
Source: https://github.com/grpc/grpc/blob/master/examples/python/helloworld/greeter_server.py
"""

from concurrent import futures
import logging

import grpc

import test_service_pb2
import test_service_pb2_grpc


class Greeter(test_service_pb2_grpc.GreeterServicer):
    def SayHello(self, request, context):
        return test_service_pb2.HelloReply(message=f'Hello, {request.name}!')


def serve():
    server = grpc.server(futures.ThreadPoolExecutor(max_workers=10))
    test_service_pb2_grpc.add_GreeterServicer_to_server(Greeter(), server)
    server.add_insecure_port('[::]:50051')
    server.start()
    server.wait_for_termination()


if __name__ == '__main__':
    logging.basicConfig()
    serve()
