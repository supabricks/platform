-- Root reclamation must not parse every retained publication for each root on
-- every daemon tick. Keep snapshot state and active-writer fencing unchanged.
CREATE INDEX publication_storage_generation
 ON publications(json_extract(descriptor,'$.generation'), export_id);
