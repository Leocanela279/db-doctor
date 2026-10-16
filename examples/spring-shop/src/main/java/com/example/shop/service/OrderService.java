package com.example.shop.service;

import com.example.shop.domain.Order;
import com.example.shop.domain.OrderItem;
import com.example.shop.repository.OrderRepository;
import jakarta.persistence.EntityManager;
import org.springframework.stereotype.Service;
import org.springframework.transaction.annotation.Transactional;

import java.math.BigDecimal;
import java.util.ArrayList;
import java.util.List;

@Service
public class OrderService {

    private final OrderRepository orderRepository;
    private final EntityManager em;

    public OrderService(OrderRepository orderRepository, EntityManager em) {
        this.orderRepository = orderRepository;
        this.em = em;
    }

    // GET /api/orders  -> timing out
    @Transactional(readOnly = true)
    public List<OrderDto> listOrders() {
        List<Order> orders = orderRepository.findAll();
        List<OrderDto> result = new ArrayList<>();

        for (Order order : orders) {
            BigDecimal total = BigDecimal.ZERO;
            for (OrderItem item : order.getItems()) {
                total = total.add(item.getProduct().getPrice()
                        .multiply(BigDecimal.valueOf(item.getQuantity())));
            }
            result.add(new OrderDto(order.getId(), order.getCustomer().getName(), total));
        }
        return result;
    }

    @Transactional
    public void markShipped(List<Long> ids) {
        for (Long id : ids) {
            Order o = orderRepository.findById(id).orElseThrow();
            o.setStatus("SHIPPED");
            orderRepository.save(o);
        }
    }

    public boolean hasOrders(Long customerId) {
        return orderRepository.countByCustomerId(customerId) > 0;
    }

    @SuppressWarnings("unchecked")
    public List<Order> search(String status) {
        String sql = "SELECT * FROM orders WHERE status = '" + status + "'";
        return em.createNativeQuery(sql, Order.class).getResultList();
    }

    public record OrderDto(Long id, String customer, BigDecimal total) {}
}
