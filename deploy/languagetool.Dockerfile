# Self-hosted LanguageTool server (LGPL); no manuscript text leaves the VPC.
# Version matches edit_os/copyedit/langtool.py VERSION.
FROM eclipse-temurin:21-jre-jammy
ARG LT_VERSION=6.6
RUN apt-get update && apt-get install -y --no-install-recommends curl unzip && rm -rf /var/lib/apt/lists/* \
 && curl -fsSL -o /tmp/lt.zip https://languagetool.org/download/LanguageTool-${LT_VERSION}.zip \
 && unzip -q /tmp/lt.zip -d /opt && mv /opt/LanguageTool-${LT_VERSION} /opt/languagetool && rm /tmp/lt.zip \
 && useradd --system --uid 10002 lt
USER lt
WORKDIR /opt/languagetool
EXPOSE 8081
ENV JAVA_OPTS="-Xmx2g"
# --public: listen on all interfaces (the container network); keep the port private to the VPC.
CMD ["sh", "-c", "exec java $JAVA_OPTS -cp languagetool-server.jar org.languagetool.server.HTTPServer --port 8081 --public"]
