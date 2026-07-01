pipeline {
    agent any

    options {
        timestamps()
        ansiColor('xterm')
        disableConcurrentBuilds()
        buildDiscarder(logRotator(numToKeepStr: '20'))
    }

    environment {
        IMAGE_NAME = 'template-maker'
        IMAGE_TAG  = "${env.BUILD_NUMBER}"
        PIP_NO_CACHE_DIR = '1'
    }

    stages {
        stage('Checkout') {
            steps { checkout scm }
        }

        stage('Setup Python') {
            steps {
                sh '''
                    python3.11 -m venv .venv
                    . .venv/bin/activate
                    pip install --upgrade pip
                    pip install -r requirements.txt
                '''
            }
        }

        stage('Lint') {
            steps {
                sh '''
                    . .venv/bin/activate
                    ruff check app tests
                '''
            }
        }

        stage('Tests') {
            steps {
                sh '''
                    . .venv/bin/activate
                    pytest -q --maxfail=1
                '''
            }
        }

        stage('JS Tests') {
            steps {
                // Pure client-side dynamic-field logic (tests/js) on Node's
                // built-in runner (no deps). Skips with a warning when the agent
                // has no Node, so the pipeline stays green on such agents.
                sh '''
                    if command -v node >/dev/null 2>&1; then
                        node --test tests/js/*.test.js
                    else
                        echo "WARNING: node not found — skipping JS tests"
                    fi
                '''
            }
        }

        stage('Build image') {
            steps {
                sh 'docker build -f docker/Dockerfile -t ${IMAGE_NAME}:${IMAGE_TAG} -t ${IMAGE_NAME}:latest .'
            }
        }

        stage('Push image') {
            when { branch 'main' }
            steps {
                withCredentials([usernamePassword(credentialsId: 'docker-registry', usernameVariable: 'REG_USER', passwordVariable: 'REG_PASS')]) {
                    sh '''
                        echo "$REG_PASS" | docker login -u "$REG_USER" --password-stdin
                        docker push ${IMAGE_NAME}:${IMAGE_TAG}
                        docker push ${IMAGE_NAME}:latest
                    '''
                }
            }
        }
    }

    post {
        always {
            sh 'rm -rf .venv || true'
        }
    }
}
