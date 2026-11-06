from django.contrib.auth.hashers import Argon2PasswordHasher


class TunedArgon2PasswordHasher(Argon2PasswordHasher):
    """Argon2id with OWASP's recommended minimum: 19 MiB of memory, 2 passes, 1 lane.

    Django's default Argon2 settings use 100 MiB per check, which is slow on a small
    shared server. These settings still make a stolen password database very costly
    to crack, while a login check takes a few tens of milliseconds. The algorithm name
    stays "argon2", so the stored hashes are standard Argon2 hashes.
    """
    time_cost = 2
    memory_cost = 19456
    parallelism = 1
