.PHONY: build up down logs test shell clean

build:
	docker compose build

up:
	docker compose up -d --build

down:
	docker compose down

logs:
	docker compose logs -f --tail=200

test:
	docker compose run --rm --no-deps clipper pytest -q

shell:
	docker compose exec clipper sh

clean:
	docker compose down -v
