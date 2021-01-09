# jwt-postgresql-proxy

PostgreSQL proxy that puts stateless JWT authentication in front of PostgreSQL

> This is a work-in-progress. This README serves as a rough design spec.


## Use case

You have a PostgreSQL database, and would like to frequently issue temporary credentials to a set of users, where a single real-world user can have a number of temporary credentials issued at any given moment. This can be done just with PostgreSQL, but involves work-arounds:

- GRANTing multiple permissions at the same time can result in "tuple concurrently updated" errors, requiring explicit locking to avoid, and so can be slow when there are multiple users attempting to get credentials for a high number of database objects at any one time.

- For objects created by the temporary database users, ownership has to be transferred, for example by database triggers, to a permanent role for the real-world user.

This proxy avoids having to do the above workarounds:

- Database credentials are issued as a temporary stateless JWT token, by code that holds a private key.

- Instead of connecting directly to the database, users connect to this proxy. It verifies the credentials using the corresponding public key, and connects to the database as the permanent database user, the the credentials of which the real-world user never knows.

The JWT token being _stateless_ means that the issuer of credentials does not need to communicate with the proxy via some internal API, and this proxy does not need a database to store credentials.


## Usage

An Ed25519 public/private key pair needs to be created, for example using the [Python cryptography package](https://github.com/pyca/cryptography):

```python
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, NoEncryption, PrivateFormat, PublicFormat

private_key = Ed25519PrivateKey.generate()
print(private_key.private_bytes(encoding=Encoding.PEM, format=PrivateFormat.PKCS8, encryption_algorithm=NoEncryption()))
print(private_key.public_key().public_bytes(encoding=Encoding.PEM, format=PublicFormat.SubjectPublicKeyInfo))
```

The issers of credentials would use the private key to create a JWT for a database user, such as in the below Python example for the database user `my_user` for the next 24 hours.

```python
from base64 import b64encode
import json
import time
from cryptography.hazmat.backends import default_backend
from cryptography.hazmat.primitives.serialization import load_pem_private_key

def b64encode_nopadding(to_encode):
	return b64encode(to_encode).rstrip(b'=')

private_key = load_pem_private_key(
	# In real cases, take the private key from an environment variable or secret store
    b'-----BEGIN PRIVATE KEY-----\n' \
    b'MC4CAQAwBQYDK2VwBCIEINQG5lNt1bE8TZa68mV/WZdpqsXaOXBHvgPQGm5CcjHp\n' \
    b'-----END PRIVATE KEY-----\n', password=None, backend=default_backend())
header = {
    'typ': 'JWT',
    'alg': 'EdDSA',
    'crv': 'Ed25519',
}
payload = {
    'sub': 'my_user',
    'exp': int(time.time() + 60 * 60 * 24),
}
to_sign = b64encode_nopadding(json.dumps(header).encode('utf-8')) + b'.' + b64encode_nopadding(json.dumps(header).encode('utf-8'))
signature = b64encode_nopadding(private_key.sign(to_sign))
jwt = (to_sign + b'.' + signature).decode()
print(jwt)
```

The JWT can be given to the real-world user, and used as the PostgreSQL password to connect to the proxy as `my-user`.

```python
import psycopg2

jwt = 'eyJ0eXAiOiAiSldUIiwgImFsZyI6ICJFZERTQSIsICJjcnYiOiAiRWQyNTUxOSJ9.eyJ0eXAiOiAiSldUIiwgImFsZyI6ICJFZERTQSIsICJjcnYiOiAiRWQyNTUxOSJ9.pkV3ZTyWC8aF7xj/Dxde6aZiehYfEiV5cEIF8iFHmiJxgPQbifvM6mWo2FHTuM85r5zidb6FkIs747DD4xhIAw'
conn = psycopg2.connect(password=jwt, user='my_user', host='host-of-the-proxy', dbname='my_dbname', port=5432)
```

## Development environment

```
python3 -m venv env
source env/bin/activate
```
