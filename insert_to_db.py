import os
import subprocess
import pymysql.cursors
from dotenv import load_dotenv
from tqdm import tqdm

load_dotenv('.env')


def main():
    connection = pymysql.connect(
        host=os.environ.get('DB_HOST'),
        user=os.environ.get('DB_USER'),
        password=os.environ.get('DB_PASS'),
        database=os.environ.get('DB_NAME'),
        port=int(os.environ.get('DB_PORT'))
    )

    with connection.cursor() as cursor:
        sql = """
            SELECT DISTINCT component
            FROM third_party.third_party_components
            WHERE url_for_metadata NOT LIKE %s
        """
        cursor.execute(sql, [f'%flow%'])
        component_names = cursor.fetchall()
        print(f"found {len(component_names)} components")
        
        for component in tqdm(component_names, desc="Processing components", ncols=80):
            component_name = component[0]

            # run the script to generate dependencies to this particular component and insert into the db
            try:
                result = subprocess.run([
                    "py",
                    "generate_dependencies.py",
                    r"C:\Users\rhuang\desktop\code\fme\workspaces\everything.sln",
                    "--root",
                    component_name,
                    "--direction",
                    "down"
                ], check=True)

            except subprocess.CalledProcessError as e:
                print(f"Error occurred while processing component: {component_name}")
                print(f"Command that caused the error: {e.cmd}")
                print(f"Return code: {e.returncode}")
                print(f"Output: {e.output}")
                print(f"Stderr: {e.stderr}")

    connection.close()

if __name__ == "__main__":
    main()
