#
# Makefile for docker_volume_manager deploy with Docker
# Version:	1.0 (20250301)
# Authors:	Lisardo Prieto
#

##################################################
# GLOBAL VARIABLES
##################################################

# lpgonzalez/docker_volume_manager:1.0

image-name = docker_volume_manager
image-version = 1.0

####################

container-dvm = docker-volume-manager

####################



# Source to backup (docker volume, local dir, etc.)
######################################################

backup-source-ext-path = ${PWD}/in_dir
backup-source-int-path = /app/input_dir


# Backup destination (output)
######################################################

backup-destination-ext-path = ${PWD}/out_dir
backup-destination-int-path = /app/output_dir


# Logs
######################################################

logs-ext-path = ${PWD}/logs
logs-int-path = /app/logs
log-level = INFO
log-level-debug = DEBUG
log-output = 'console,file,json_file'



##################################################



update:
	docker pull $(image-name):$(image-version)



init:
	make clean
	make update
	make deploy



deploy:
	make clean

	docker run -d \
		--name=$(container-dvm) \
		-v /etc/timezone:/etc/timezone:ro \
		-v /etc/localtime:/etc/localtime:ro \
		-v $(backup-source-ext-path):$(backup-source-int-path) \
		-v $(backup-destination-ext-path):$(backup-destination-int-path) \
		-e TZ=Europe/Madrid \
		-e LOG_LEVEL=$(log-level) \
		$(image-name):$(image-version)



devel:
	make clean

	docker run \
		--name=$(container-dvm) \
		-v ${PWD}/app:/app \
		-v /etc/timezone:/etc/timezone:ro \
		-v /etc/localtime:/etc/localtime:ro \
		-v $(backup-source-ext-path):$(backup-source-int-path) \
		-v $(backup-destination-ext-path):$(backup-destination-int-path) \
		-v $(logs-ext-path):$(logs-int-path) \
		-e TZ=Europe/Madrid \
		-e LOG_LEVEL=$(log-level-debug) \
		-e LOG_OUTPUT=$(log-output) \
		-it --rm \
		$(image-name):$(image-version) bash



stop:
	# Stop container
	@echo "Stopping existing container..."
	docker stop $(container-dvm)  || echo "$(container-dvm) container not stopped"



clean:
	make stop

	# Remove container
	@echo "Cleaning existing container..."
	docker rm $(container-dvm)	|| echo "$(container-dvm) container not removed"



##################################################



bash:
	docker exec -it $(container-dvm) bash



##################################################



build:
	docker build --tag $(image-name):$(image-version) .
	docker tag $(image-name):$(image-version) $(image-name):latest



push-to-repo:
	docker push -a $(image-name)



save-image:
	docker save $(image-name):$(image-version) | gzip > "$(image-name) v$(image-version).tar.gz"



load-image:
	docker load -i "$(image-name) v$(image-version).tar.gz"
