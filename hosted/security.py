"""Authentication primitives; never log plaintext passwords or session tokens."""
import base64
import hashlib
import hmac
import re
import secrets


class ServiceError(ValueError):
    def __init__(self,message,status=400):
        super().__init__(message);self.status=status


def digest(value):
    return hashlib.sha256(value.encode('utf-8')).hexdigest()


def password_hash(password):
    if not isinstance(password,str) or not 12<=len(password)<=128:
        raise ServiceError('Use a password between 12 and 128 characters.')
    salt=secrets.token_bytes(16)
    result=hashlib.pbkdf2_hmac('sha256',password.encode(),salt,600_000)
    return 'pbkdf2-sha256$600000$'+salt.hex()+'$'+result.hex()


def password_valid(password,stored):
    try:
        method,iterations,salt,expected=stored.split('$')
        if method!='pbkdf2-sha256' or not 600_000<=int(iterations)<=2_000_000 or not isinstance(password,str) or len(password)>128:
            return False
        actual=hashlib.pbkdf2_hmac('sha256',password.encode(),bytes.fromhex(salt),int(iterations)).hex()
        return hmac.compare_digest(actual,expected)
    except (ValueError,TypeError):return False


def email_address(value):
    if not isinstance(value,str):raise ServiceError('Enter a valid email address.')
    value=value.strip().casefold()
    if len(value)>254 or not re.fullmatch(r'[^\s@]+@[^\s@]+\.[^\s@]+',value):
        raise ServiceError('Enter a valid email address.')
    return value


def identifier(value,label='Identifier'):
    if not isinstance(value,str) or not 1<=len(value)<=150 or not re.fullmatch(r'[A-Za-z0-9_-]+',value):
        raise ServiceError(label+' is invalid.')
    return value


def amount(value):
    if isinstance(value,bool) or not isinstance(value,int) or not 0<value<=100_000_000:
        raise ServiceError('Provide a positive integer amount in cents, at most $1,000,000.')
    return value


def webhook_valid(signature,notification_url,raw_body,key):
    if not key or not isinstance(signature,str) or len(signature)!=44 or not signature.isascii():return False
    expected=base64.b64encode(hmac.new(key.encode(),notification_url.encode()+raw_body,hashlib.sha256).digest()).decode()
    return hmac.compare_digest(signature,expected)
