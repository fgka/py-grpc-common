# Python code 

This code was generated as follows:

Go to `integ_tests` directory:

```bash
cd /path/to/py-grpc-common/integ_tests
```

Generate code with:

```bash
python -m grpc_tools.protoc \
  --proto_path=./protos \
  --python_out=. \
  --grpc_python_out=. \
  ./protos/test_service.proto
```

To understand why it is at `.`, read:

* [Issue 1491](https://github.com/protocolbuffers/protobuf/issues/1491)
* [Comment on package and Python](https://github.com/grpc/grpc/issues/2010#issuecomment-110495155)