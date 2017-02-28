# Copyright 2014 Netflix, Inc.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

FROM ubuntu:14.04
MAINTAINER Netflix Open Source Development <talent@netflix.com>

ENV SECURITY_MONKEY_VERSION=v0.8.0 \
    SECURITY_MONKEY_SETTINGS=/usr/local/src/security_monkey/env-config/config-docker.py

RUN apt-get update &&\
  apt-get -y -q install python-software-properties software-properties-common postgresql-9.3 postgresql-client-9.3 postgresql-contrib-9.3 curl git sudo apt-transport-https &&\
  apt-get install -y python-pip python-dev python-psycopg2 libffi-dev libpq-dev libyaml-dev libxml2-dev libxmlsec1-dev git sudo swig nginx &&\
  curl https://dl-ssl.google.com/linux/linux_signing_key.pub | apt-key add - &&\
  curl https://storage.googleapis.com/download.dartlang.org/linux/debian/dart_stable.list > /etc/apt/sources.list.d/dart_stable.list && \
  apt-get update &&\
  apt-get install -y -q dart &&\
  rm -rf /var/lib/apt/lists/*

RUN pip install setuptools --upgrade

RUN cd /usr/local/src &&\
#   git clone --branch $SECURITY_MONKEY_VERSION https://github.com/Netflix/security_monkey.git
  /bin/mkdir -p security_monkey
ADD . /usr/local/src/security_monkey

RUN cd /usr/local/src/security_monkey &&\
  python setup.py install &&\
  /bin/mkdir -p /var/log/security_monkey/

RUN chmod +x /usr/local/src/security_monkey/docker/*.sh &&\
  mkdir -pv /var/log/security_monkey &&\
  /usr/bin/touch /var/log/security_monkey/securitymonkey.log
  # ln -s /dev/stdout /var/log/security_monkey/securitymonkey.log

RUN cd /usr/local/src &&\
#   git clone -b $SECURITY_MONKEY_VERSION https://github.com/Netflix/security_monkey.git
  mkdir -p security_monkey
ADD . /usr/local/src/security_monkey

RUN cd /usr/local/src/security_monkey/dart &&\
  /usr/lib/dart/bin/pub get &&\
  /usr/lib/dart/bin/pub build &&\
  /bin/mkdir -p /usr/local/src/security_monkey/security_monkey/static/ &&\
  /bin/cp -R /usr/local/src/security_monkey/dart/build/web/* /usr/local/src/security_monkey/security_monkey/static/

RUN /bin/mkdir -p /var/log/security_monkey/ /etc/nginx/ssl/ &&\
  ln -s /dev/stdout /var/log/security_monkey/security_monkey.access.log &&\
  ln -s /dev/stderr /var/log/security_monkey/security_monkey.error.log

ADD docker/nginx/securitymonkey.conf /etc/nginx/conf.d/securitymonkey.conf
COPY docker/nginx/nginx.conf /etc/nginx/nginx.conf
# ADD docker/nginx/server.crt docker/nginx/server.key /etc/nginx/ssl/

WORKDIR /usr/local/src/security_monkey
EXPOSE 5000

ENTRYPOINT ["/usr/local/src/security_monkey/docker/api-start.sh"]
