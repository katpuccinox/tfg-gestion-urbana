FROM apache/hive:4.0.0

USER root
ADD https://jdbc.postgresql.org/download/postgresql-42.7.4.jar /opt/hive/lib/postgresql-42.7.4.jar
ADD https://repo1.maven.org/maven2/com/amazonaws/aws-java-sdk-bundle/1.11.1026/aws-java-sdk-bundle-1.11.1026.jar /opt/hive/lib/aws-java-sdk-bundle-1.11.1026.jar
ADD core-site.xml /opt/hadoop/etc/hadoop/core-site.xml
RUN chmod 644 /opt/hive/lib/postgresql-42.7.4.jar \
	&& chmod 644 /opt/hive/lib/aws-java-sdk-bundle-1.11.1026.jar \
	&& cp /opt/hive/lib/postgresql-42.7.4.jar /opt/hadoop/share/hadoop/common/lib/postgresql-42.7.4.jar \
	&& cp /opt/hive/lib/aws-java-sdk-bundle-1.11.1026.jar /opt/hadoop/share/hadoop/common/lib/aws-java-sdk-bundle-1.11.1026.jar \
	&& cp /opt/hadoop/share/hadoop/tools/lib/hadoop-aws-3.3.6.jar /opt/hadoop/share/hadoop/common/lib/hadoop-aws-3.3.6.jar \
	&& chmod 644 /opt/hadoop/share/hadoop/common/lib/postgresql-42.7.4.jar /opt/hadoop/share/hadoop/common/lib/aws-java-sdk-bundle-1.11.1026.jar /opt/hadoop/share/hadoop/common/lib/hadoop-aws-3.3.6.jar
USER hive
