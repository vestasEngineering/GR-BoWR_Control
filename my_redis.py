import redis

redis_instance = redis.Redis(host='localhost', port=6379, decode_responses=True)


