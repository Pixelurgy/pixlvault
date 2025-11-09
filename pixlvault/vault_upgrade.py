from pixlvault.schema_version import SchemaVersion

from .logging import get_logger


class VaultUpgrade:
    """
    Handles schema upgrades for the Vault database.

    Attributes:
        connection: SQLite connection object.
        schema_version: SchemaVersion instance for managing schema version.
    """

    def __init__(self, connection):
        """
        Initialize a VaultUpgrade instance.

        Args:
            connection: SQLite connection object.
        """
        self.logger = get_logger(__name__)
        self.connection = connection
        self.schema_version = SchemaVersion(connection)
        self.logger.debug(
            f"Current schema version: {self.schema_version.get_version()}"
        )

    def upgrade_if_necessary(self):
        """
        Perform schema upgrade if necessary. Bumps schema version.
        """
        current_version = self.schema_version.get_version()

        # Version 2: Add picture_sets and picture_set_members tables
        if current_version < 2:
            self.logger.info("Upgrading database schema to version 2 (picture_sets)...")
            self._upgrade_to_v2()
            self.schema_version.set_version(2)
            self.logger.info("Database schema upgraded to version 2")

        # Ensure every character has a reference picture set
        self._ensure_reference_picture_sets()

    def _ensure_reference_picture_sets(self):
        self.logger.info("Ensuring reference picture sets for all characters...")
        cursor = self.connection.cursor()
        # Get all characters
        cursor.execute("SELECT id FROM characters")
        character_ids = [row["id"] for row in cursor.fetchall()]
        for char_id in character_ids:
            # Check if reference picture set exists for this character
            cursor.execute(
                "SELECT id FROM picture_sets WHERE name = ? AND description = ?",
                ("reference_pictures", str(char_id)),
            )
            result = cursor.fetchone()
            if not result:
                # Create reference picture set for this character
                cursor.execute(
                    "INSERT INTO picture_sets (name, description) VALUES (?, ?)",
                    ("reference_pictures", str(char_id)),
                )
                self.logger.info(
                    f"Created reference picture set for character id={char_id}"
                )
        self.connection.commit()

    def _upgrade_to_v2(self):
        """Add picture_sets and picture_set_members tables."""
        self.logger.info("Creating picture_sets table...")
        self.connection.execute("""
            CREATE TABLE IF NOT EXISTS picture_sets (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT,
                description TEXT
            )
        """)

        self.connection.execute("""
            CREATE INDEX IF NOT EXISTS idx_picture_sets_name ON picture_sets(name)
        """)

        self.logger.info("Creating picture_set_members table...")
        self.connection.execute("""
            CREATE TABLE IF NOT EXISTS picture_set_members (
                set_id INTEGER NOT NULL,
                picture_id TEXT NOT NULL,
                PRIMARY KEY (set_id, picture_id),
                FOREIGN KEY (set_id) REFERENCES picture_sets(id),
                FOREIGN KEY (picture_id) REFERENCES pictures(id)
            )
        """)

        self.connection.execute("""
            CREATE INDEX IF NOT EXISTS idx_picture_set_members_set_id ON picture_set_members(set_id)
        """)

        self.connection.execute("""
            CREATE INDEX IF NOT EXISTS idx_picture_set_members_picture_id ON picture_set_members(picture_id)
        """)

        self.connection.commit()
        self.logger.info("Picture sets tables created successfully")
