VERSIONS = [
    ("26.5.3", "quay.io/keycloak/keycloak:26.5.3"),
    ("26.5.4", "quay.io/keycloak/keycloak:26.5.4"),
    ("26.5.5", "quay.io/keycloak/keycloak:26.5.5"),
    ("nightly", "quay.io/keycloak/keycloak:nightly"),
]
REALM_COUNT = 10
CONCURRENCY = 20
KEYCLOAK_URL = "http://localhost:8080"
KEYCLOAK_MANAGEMENT_URL = "http://localhost:9000"
ADMIN_USER = "admin"
ADMIN_PASSWORD = "admin"
RESULTS_DIR = "results"
DB_SERVICE = "db"
COMPOSE_CMD = "docker-compose"
DOCKER_HOST = "unix:///var/folders/89/7v92vh1x0xz1b49dgtn9hxmw0000gn/T/podman/podman-machine-default-api.sock"
