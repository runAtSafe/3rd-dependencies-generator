pipeline {
    agent {
        docker {
            args '-u 0:0'
            label 'ub16-m5xlarge'
            image 'docker.arty-1.base.safe.com/devops/bar-python:v0.9.2'
        }
    }
    environment {
        BUILDSTRING_H = "foundation/util/general/buildstring.h"
        BUILDSTRING_CS = "foundation/util/general/buildstring.cs"
        FMEVERSION_H = "foundation/util/general/fmeversion.h"
        ENDPOINT_COVEO_UPDATE = "https://bar-server/fmerest/v3/transformations/submit/Coveo/coveo_thesaurus_alias_update.fmw"
        ENDPOINT_DIGITAL_COMPONENT_UPDATE="http://set-fmeserver.base.safe.com/fmerest/v3/transformations/transact/Component/MigrateDigitalComponentSchema.fmw"
    }
    stages {
        stage('Install dependencies') {
            steps {
                sh "py -m pip install --upgrade pip"
                sh "py -m pip install -r requirements.txt"
            }
        }
        stage('git clone fme repo') {
            steps {
                withCredentials([string(credentialsId: 'cjoc_bbuilders_github_token', variable: 'token')]) {
                    sh """  git clone --depth 1 https://${token}@gtb-1.base.safe.com/jheidema/fme fme
                            cd fme
                            git config --global user.name "jheidema"
                            git config --global user.email "justin.heidema@safe.com"
                            git remote add upstream https://${token}@gtb-1.base.safe.com/Safe/fme.git
                            git fetch upstream ${FME_MAJOR_VERSION}
                            git checkout ${FME_MAJOR_VERSION}
                            git checkout -b automated_build_rollover_${params.RELEASE_BUILD}_${uuid}
                            cd ..
                            python ${WORKSPACE}/jenkins-pipelines/release/build_version_rollover.py -v ${params.BUILD_ROLLOVER_VERSION}
                            cd fme
                            git add ${env.BUILDSTRING_H}
                            git add ${env.BUILDSTRING_CS}
                            git add ${env.FMEVERSION_H}
                            git commit -m \"Internal: Build rollover from ${params.RELEASE_VERSION} to ${params.BUILD_ROLLOVER_VERSION}\"
                            git push https://${token}@gtb-1.base.safe.com/jheidema/fme automated_build_rollover_${params.RELEASE_BUILD}_${uuid}
                        """
                    }
                }
            }
        }
        stage('run scripts') {
            steps {
                sh "py insert_to_db.py"
            }
        }
    }
}