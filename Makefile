# Linux and macOS convenience targets. On Windows use scripts/dev.ps1, which does the same thing.
.PHONY: up down reset backend frontend samples build

up:
	docker compose --profile core up -d
	@until docker exec sentinel-postgres pg_isready -U sentinel -d sentinel >/dev/null 2>&1; do sleep 2; done
	@echo "PostgreSQL is ready."

down:
	docker compose --profile core --profile samples down

# Removes the data volume, so schemas and seeds are recreated from scratch on the next start.
reset:
	docker compose --profile core --profile samples down -v

backend:
	dotnet run --project backend/src/Sentinel.Api

frontend:
	cd frontend && npm run dev

# postgres lives in the core profile, and every sample depends on it, so both profiles
# have to be named or compose rejects the project.
samples:
	docker compose --profile core --profile samples up -d --build

build:
	dotnet build backend/Sentinel.sln --warnaserror
	dotnet build sample-services/Sentinel.Samples.sln --warnaserror
	cd frontend && npm run build
