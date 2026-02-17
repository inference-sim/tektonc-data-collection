#!/bin/bash

# BLIS Pipeline Monitor
# Monitors pipeline execution and downloads results on completion

# Configuration
PIPELINERUN="blis-inference-perf-20260217-113113"
NAMESPACE="diya"
RESULTS_DIR="results/${PIPELINERUN}"
MONITORING_LOG="${RESULTS_DIR}/monitoring.log"
MAX_ITERATIONS=360  # 6 hours at 60 second intervals
SLEEP_INTERVAL=60

# Colors
GREEN='\033[0;32m'
RED='\033[0;31m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
CYAN='\033[1;36m'
MAGENTA='\033[0;35m'
NC='\033[0m'

# Create results directory
mkdir -p "${RESULTS_DIR}"

# Initialize log
echo "=== Pipeline Monitoring Started ===" | tee -a "${MONITORING_LOG}"
echo "Pipeline: ${PIPELINERUN}" | tee -a "${MONITORING_LOG}"
echo "Namespace: ${NAMESPACE}" | tee -a "${MONITORING_LOG}"
echo "Start Time: $(date)" | tee -a "${MONITORING_LOG}"
echo "========================================" | tee -a "${MONITORING_LOG}"
echo "" | tee -a "${MONITORING_LOG}"

# Monitoring loop
iteration=0
while [ $iteration -lt $MAX_ITERATIONS ]; do
    iteration=$((iteration + 1))
    timestamp=$(date '+%Y-%m-%d %H:%M:%S')

    echo -e "[${timestamp}] ${BLUE}Check ${iteration}/${MAX_ITERATIONS}${NC}" | tee -a "${MONITORING_LOG}"

    # Get status
    status=$(tkn pr describe ${PIPELINERUN} -n ${NAMESPACE} -o json 2>/dev/null | jq -r '.status.conditions[] | select(.type=="Succeeded") | .status' 2>/dev/null)

    if [ -z "$status" ] || [ "$status" == "null" ]; then
        status="Unknown"
    fi

    echo -e "  Status: ${status}" | tee -a "${MONITORING_LOG}"

    # Check for terminal state
    if [ "$status" == "True" ]; then
        echo -e "${GREEN}━━━ Pipeline SUCCEEDED ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}" | tee -a "${MONITORING_LOG}"
        echo -e "${GREEN}✓${NC} ${PIPELINERUN} completed successfully" | tee -a "${MONITORING_LOG}"
        echo "" | tee -a "${MONITORING_LOG}"

        # Get full pipeline description
        echo "=== Final Pipeline Details ===" | tee -a "${MONITORING_LOG}"
        tkn pr describe ${PIPELINERUN} -n ${NAMESPACE} | tee -a "${MONITORING_LOG}"
        echo "" | tee -a "${MONITORING_LOG}"

        # Download results
        echo "=== Downloading Results ===" | tee -a "${MONITORING_LOG}"
        echo -e "${BLUE}⠋${NC} Creating temporary pod..." | tee -a "${MONITORING_LOG}"

        # Create temporary pod
        kubectl run data-copy-temp --image=busybox --restart=Never \
          --overrides='{"spec":{"containers":[{"name":"data-copy","image":"busybox","command":["sleep","300"],"volumeMounts":[{"name":"data","mountPath":"/data"}]}],"volumes":[{"name":"data","persistentVolumeClaim":{"claimName":"data-pvc"}}]}}' \
          -n ${NAMESPACE} 2>&1 | tee -a "${MONITORING_LOG}"

        # Wait for pod to be ready
        echo -e "${BLUE}⠋${NC} Waiting for pod to be ready..." | tee -a "${MONITORING_LOG}"
        if kubectl wait --for=condition=Ready pod/data-copy-temp -n ${NAMESPACE} --timeout=60s 2>&1 | tee -a "${MONITORING_LOG}"; then

            # Copy data
            echo -e "${BLUE}⠋${NC} Copying data from cluster..." | tee -a "${MONITORING_LOG}"
            kubectl cp ${NAMESPACE}/data-copy-temp:/data/${PIPELINERUN}-1/ ${RESULTS_DIR}/ 2>&1 | tee -a "${MONITORING_LOG}"

            if [ $? -eq 0 ]; then
                echo -e "${GREEN}✓${NC} Data downloaded to ${RESULTS_DIR}" | tee -a "${MONITORING_LOG}"
                echo "" | tee -a "${MONITORING_LOG}"
                echo "Downloaded files:" | tee -a "${MONITORING_LOG}"
                ls -lh ${RESULTS_DIR}/ | tee -a "${MONITORING_LOG}"
            else
                echo -e "${RED}✗${NC} Failed to download data" | tee -a "${MONITORING_LOG}"
            fi
        else
            echo -e "${RED}✗${NC} Pod failed to become ready" | tee -a "${MONITORING_LOG}"
        fi

        # Cleanup
        echo "" | tee -a "${MONITORING_LOG}"
        echo -e "${BLUE}⠋${NC} Cleaning up temporary pod..." | tee -a "${MONITORING_LOG}"
        kubectl delete pod data-copy-temp -n ${NAMESPACE} --wait=false 2>&1 | tee -a "${MONITORING_LOG}"

        echo "" | tee -a "${MONITORING_LOG}"
        echo -e "${GREEN}━━━ Monitoring Complete ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}" | tee -a "${MONITORING_LOG}"
        echo "End Time: $(date)" | tee -a "${MONITORING_LOG}"
        exit 0

    elif [ "$status" == "False" ]; then
        echo -e "${RED}━━━ Pipeline FAILED ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}" | tee -a "${MONITORING_LOG}"
        echo -e "${RED}✗${NC} ${PIPELINERUN} failed" | tee -a "${MONITORING_LOG}"
        echo "" | tee -a "${MONITORING_LOG}"

        # Get full pipeline description
        echo "=== Final Pipeline Details ===" | tee -a "${MONITORING_LOG}"
        tkn pr describe ${PIPELINERUN} -n ${NAMESPACE} | tee -a "${MONITORING_LOG}"

        # Get failed task logs
        echo "" | tee -a "${MONITORING_LOG}"
        echo "=== Failed Task Logs (last 100 lines) ===" | tee -a "${MONITORING_LOG}"
        tkn pr logs ${PIPELINERUN} -n ${NAMESPACE} 2>&1 | tail -100 | tee -a "${MONITORING_LOG}"

        echo "" | tee -a "${MONITORING_LOG}"
        echo "=== Monitoring Complete ===" | tee -a "${MONITORING_LOG}"
        echo "End Time: $(date)" | tee -a "${MONITORING_LOG}"
        exit 1

    else
        # Still running, show current task status
        current_tasks=$(tkn pr describe ${PIPELINERUN} -n ${NAMESPACE} -o json 2>/dev/null | \
          jq -r '.status.taskRuns | to_entries[] | select(.value.status.conditions[].status=="Unknown") | .value.pipelineTaskName' 2>/dev/null)

        completed_tasks=$(tkn pr describe ${PIPELINERUN} -n ${NAMESPACE} -o json 2>/dev/null | \
          jq -r '.status.taskRuns | to_entries[] | select(.value.status.conditions[].status=="True") | .value.pipelineTaskName' 2>/dev/null | wc -l)

        if [ ! -z "$current_tasks" ]; then
            echo -e "  ${CYAN}Running:${NC} $(echo $current_tasks | head -1)" | tee -a "${MONITORING_LOG}"
        fi
        echo -e "  ${CYAN}Completed:${NC} ${completed_tasks} tasks" | tee -a "${MONITORING_LOG}"
    fi

    echo "" | tee -a "${MONITORING_LOG}"

    # Sleep before next check
    if [ $iteration -lt $MAX_ITERATIONS ]; then
        sleep ${SLEEP_INTERVAL}
    fi
done

# Timeout reached
echo -e "${YELLOW}━━━ Monitoring Timeout ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}" | tee -a "${MONITORING_LOG}"
echo -e "${YELLOW}⚠${NC} Monitoring timeout reached (6 hours)" | tee -a "${MONITORING_LOG}"
echo "Pipeline may still be running." | tee -a "${MONITORING_LOG}"
echo "Check status with: tkn pr describe ${PIPELINERUN} -n ${NAMESPACE}" | tee -a "${MONITORING_LOG}"
echo "End Time: $(date)" | tee -a "${MONITORING_LOG}"
exit 3
