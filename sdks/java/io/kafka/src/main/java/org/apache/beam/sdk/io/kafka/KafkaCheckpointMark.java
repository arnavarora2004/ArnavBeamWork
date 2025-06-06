/*
 * Licensed to the Apache Software Foundation (ASF) under one
 * or more contributor license agreements.  See the NOTICE file
 * distributed with this work for additional information
 * regarding copyright ownership.  The ASF licenses this file
 * to you under the Apache License, Version 2.0 (the
 * "License"); you may not use this file except in compliance
 * with the License.  You may obtain a copy of the License at
 *
 *     http://www.apache.org/licenses/LICENSE-2.0
 *
 * Unless required by applicable law or agreed to in writing, software
 * distributed under the License is distributed on an "AS IS" BASIS,
 * WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
 * See the License for the specific language governing permissions and
 * limitations under the License.
 */
package org.apache.beam.sdk.io.kafka;

import static org.apache.beam.vendor.guava.v32_1_2_jre.com.google.common.base.Preconditions.checkState;

import java.io.Serializable;
import java.util.List;
import java.util.Optional;
import org.apache.avro.reflect.AvroIgnore;
import org.apache.beam.sdk.coders.DefaultCoder;
import org.apache.beam.sdk.extensions.avro.coders.AvroCoder;
import org.apache.beam.sdk.io.UnboundedSource;
import org.apache.beam.sdk.transforms.windowing.BoundedWindow;
import org.apache.beam.vendor.guava.v32_1_2_jre.com.google.common.base.Joiner;

/**
 * Checkpoint for a {@link KafkaUnboundedReader}. Consists of Kafka topic name, partition id, and
 * the latest offset consumed so far.
 */
@DefaultCoder(AvroCoder.class)
public class KafkaCheckpointMark implements UnboundedSource.CheckpointMark {
  private static final long OFFSET_DEDUP_PARTITIONS_PER_SPLIT = 1;

  private List<PartitionMark> partitions;

  @AvroIgnore
  private Optional<KafkaUnboundedReader<?, ?>> reader; // Present when offsets need to be committed.

  @SuppressWarnings("initialization") // Avro will set the fields by breaking abstraction
  private KafkaCheckpointMark() {} // for Avro

  public KafkaCheckpointMark(
      List<PartitionMark> partitions, Optional<KafkaUnboundedReader<?, ?>> reader) {
    this.partitions = partitions;
    this.reader = reader;
  }

  public List<PartitionMark> getPartitions() {
    return partitions;
  }

  @Override
  public void finalizeCheckpoint() {
    reader.ifPresent(r -> r.finalizeCheckpointMarkAsync(this));
    // Is it ok to commit asynchronously, or should we wait till this (or newer) is committed?
    // Often multiple marks would be finalized at once, since we only need to finalize the latest,
    // it is better to wait a little while. Currently maximum delay is same as KAFKA_POLL_TIMEOUT
    // in the reader (1 second).
  }

  @Override
  public String toString() {
    return "KafkaCheckpointMark{partitions=" + Joiner.on(",").join(partitions) + '}';
  }

  @Override
  public byte[] getOffsetLimit() {
    if (!reader.isPresent()) {
      throw new RuntimeException(
          "KafkaCheckpointMark reader is not present while calling getOffsetLimit().");
    }
    if (!reader.get().offsetBasedDeduplicationSupported()) {
      throw new RuntimeException(
          "Unexpected getOffsetLimit() called while KafkaUnboundedReader not configured for offset deduplication.");
    }

    // KafkaUnboundedSource.split() must produce a 1:1 partition to split ratio.
    checkState(partitions.size() == OFFSET_DEDUP_PARTITIONS_PER_SPLIT);
    PartitionMark partition = partitions.get(/* index= */ 0);
    return KafkaIOUtils.OffsetBasedDeduplication.encodeOffset(partition.getNextOffset());
  }

  /**
   * A tuple to hold topic, partition, and offset that comprise the checkpoint for a single
   * partition.
   */
  public static class PartitionMark implements Serializable {
    private static final long MIN_WATERMARK_MILLIS = BoundedWindow.TIMESTAMP_MIN_VALUE.getMillis();

    private String topic;
    private int partition;
    private long nextOffset;
    private long watermarkMillis = MIN_WATERMARK_MILLIS;

    @SuppressWarnings("initialization")
    private PartitionMark() {} // for Avro

    public PartitionMark(String topic, int partition, long offset, long watermarkMillis) {
      this.topic = topic;
      this.partition = partition;
      this.nextOffset = offset;
      this.watermarkMillis = watermarkMillis;
    }

    public String getTopic() {
      return topic;
    }

    public int getPartition() {
      return partition;
    }

    public long getNextOffset() {
      return nextOffset;
    }

    public long getWatermarkMillis() {
      return watermarkMillis;
    }

    @Override
    public String toString() {
      return "PartitionMark{"
          + "topic='"
          + topic
          + '\''
          + ", partition="
          + partition
          + ", nextOffset="
          + nextOffset
          + ", watermarkMillis="
          + watermarkMillis
          + '}';
    }
  }
}
