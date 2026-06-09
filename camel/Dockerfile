FROM apache/camel:4.4.0

COPY camel-atm.xml /deployments/camel-atm.xml

ENV CAMEL_ROUTE_FILE=/deployments/camel-atm.xml

CMD ["camel", "run", "/deployments/camel-atm.xml"]
