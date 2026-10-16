package com.example.shop.repository;

import com.example.shop.domain.Order;
import org.springframework.data.jpa.repository.JpaRepository;
import org.springframework.data.jpa.repository.Query;
import org.springframework.data.repository.query.Param;

import java.util.List;

public interface OrderRepository extends JpaRepository<Order, Long> {

    List<Order> findByStatus(String status);

    List<Order> findByCustomerEmailIgnoreCase(String email);

    List<Order> findByCustomerIdAndStatusOrderByCreatedAtDesc(Long customerId, String status);

    long countByCustomerId(Long customerId);

    @Query(value = "SELECT * FROM orders WHERE lower(status) = :status", nativeQuery = true)
    List<Order> findByStatusNative(@Param("status") String status);
}
