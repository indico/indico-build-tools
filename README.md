# indico-build-tools

These are some build/deployment tools for the Indico Project.


## Usage examples


Deploy indico in the production cluster:

    $ fab deploy:cluster=prod

---

Restart apache in every server in the `dev` cluster, with a 20s interval between restarts:

    $ fab cluster:dev restart_apache:t=20