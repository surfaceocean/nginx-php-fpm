-- Nextcloud 35 requires the classic hashing component with MySQL 9+.
-- The official image runs this only while initializing a new database.
INSTALL COMPONENT 'file://component_classic_hashing';
